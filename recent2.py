#!/usr/bin/env python
#https://github.com/kislyuk/argcomplete#synopsis
import argcomplete
import argparse
import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
import time
from pathlib import Path
from tabulate import tabulate

from datetime import datetime
# pip install python-dateutil
from dateutil import tz


recent_db = os.getenv('RECENT_DB', os.environ['HOME'] + '/.recent.db')
EXPECTED_PROMPT = 'log-recent -r $__bp_last_ret_value -c "$(HISTTIMEFORMAT= history 1)" -p $$'

class Term:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    LIGHTCYAN = '\033[1;36m'
    LIGHTGRAY = '\033[0;37m'
    YELLOW = '\033[0;33m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

class DB:
    SCHEMA_VERSION = 2
    CASE_ON = "PRAGMA case_sensitive_like = true"
    GET_COMMANDS_TABLE_SCHEMA = """
        select sql
        from sqlite_master
        where type = 'table' and name = 'commands'"""
    # NOTE(dotslash): I haven't found a way to send json using ?s. So doing with string formats.
    INSERT_ROW = """
        insert into commands
            (command_dt,command,pid,return_val,pwd,session,json_data)
            values (
                datetime(?, 'unixepoch'), -- command_dt
                ?, -- command
                ?, -- pid
                ?, -- return_val
                ?, -- pwd
                ?, -- session
                {} -- json_data
            )"""
    INSERT_ROW_NO_JSON = """
        insert into commands
            (command_dt,command,pid,return_val,pwd,session,json_data)
            values (
                datetime(?, 'unixepoch'), -- command_dt
                ?, -- command
                ?, -- pid
                ?, -- return_val
                ?, -- pwd
                ?, -- session
                null -- json_data
            )"""
    INSERT_SESSION = """
        insert into sessions
            (created_dt, updated_dt, term, hostname, user, sequence, session)
            values (
                datetime('now','localtime'), datetime('now','localtime'), -- created_dt, updated_dt
                ?, -- term
                ?, -- hostname
                ?, -- user
                ?, -- sequence
                ?  -- session
            )"""
    UPDATE_SESSION = """
        update sessions
        set updated_dt = datetime('now','localtime'), sequence = ?
        where session = ?"""
    # TAIL_N_ROWS's columns (column order is same as TAIL_N_ROWS
    TAIL_N_ROWS_COLUMNS = 'command_dt,command,pid,return_val,pwd,session,json_data'.split(',')
    TAIL_N_ROWS_DEDUP_COLUMNS = 'command_dt,command'.split(',')
    TAIL_N_ROWS_TEMPLATE = """
        select command_dt,command,pid,return_val,pwd,session,json_data
        from (
            select *
            from commands
            where
            order by command_dt desc limit ?
        )
        order by command_dt"""
    TAIL_N_ROWS_TEMPLATE_DEDUP = """
        select *
        from (
            select max(command_dt) as command_dt, command
            from commands
            where
            group by command
            order by command_dt desc limit ?
        )
        order by command_dt"""
    GET_SESSION_SEQUENCE = """select sequence from sessions where session = ?"""

    # Setup: Create tables.
    CREATE_COMMANDS_TABLE = """
        create table if not exists commands (
            command_dt timestamp,
            command text,
            pid int,
            return_val int,
            pwd text,
            session text,
            json_data json
        )"""
    CREATE_SESSIONS_TABLE = """
        create table if not exists sessions (
            session text primary key not null,
            created_dt timestamp,
            updated_dt timestamp,
            term text,
            hostname text,
            user text,
            sequence int
        )"""
    CREATE_DATE_INDEX = """
        create index if not exists command_dt_ind
            on commands (command_dt)"""
    # Schema version
    GET_SCHEMA_VERSION = """pragma user_version"""
    UPDATE_SCHEMA_VERSION = """pragma user_version = """
    # Migrate from v1 to v2.
    MIGRATE_1_2 = "alter table commands add column json_data json"


class Session:
    @classmethod
    def session_id_string(cls, pid=None):
        # TODO(sai): Should this always be ppid?
        pid = pid or os.getppid()
        # This combination of ENV vars *should* provide a unique session
        # TERM_SESSION_ID for OS X Terminal
        # XTERM for xterm
        # TMUX, TMUX_PANE for tmux
        # STY for GNU screen
        # SHLVL handles nested shells
        seed = "{}-{}-{}-{}-{}-{}-{}".format(
            os.getenv('TERM_SESSION_ID', ''),
            os.getenv('WINDOWID', ''),
            os.getenv('SHLVL', ''),
            os.getenv('TMUX', ''),
            os.getenv('TMUX_PANE', ''),
            os.getenv('STY', ''),
            pid,
        )  # yapf: disable
        return hashlib.md5(seed.encode('utf-8')).hexdigest()

    def __init__(self, pid, sequence):
        self.sequence = sequence
        self.empty = False
        self.id = Session.session_id_string(pid)

    def update(self, conn):
        c = conn.cursor()
        try:
            term = os.getenv('TERM', '')
            hostname = socket.gethostname()
            user = os.getenv('USER', '')
            c.execute(DB.INSERT_SESSION, [term, hostname, user, self.sequence, self.id])
            self.empty = True
        except sqlite3.IntegrityError:
            # Carriage returns need to be ignored
            expected_sequence = c.execute(DB.GET_SESSION_SEQUENCE, [self.id]).fetchone()[0]
            if expected_sequence == int(self.sequence):
                self.empty = True
            c.execute(DB.UPDATE_SESSION, [self.sequence, self.id])
        c.close()


def migrate(cur_version, conn):
    if cur_version not in (0, 1):
        exit(Term.FAIL + ('recent: your command history database does not '
                          'match recent, please update') + Term.ENDC)

    c = conn.cursor()
    if cur_version == 1:
        # Schema version is v1. Migrate to v2.
        print(Term.WARNING + 'recent: migrating schema to version {}'.format(DB.SCHEMA_VERSION) +
              Term.ENDC)
        c.execute(DB.MIGRATE_1_2)
    else:
        print(Term.WARNING + 'recent: building schema' + Term.ENDC)
        c.execute(DB.CREATE_COMMANDS_TABLE)
        c.execute(DB.CREATE_SESSIONS_TABLE)
        c.execute(DB.CREATE_DATE_INDEX)

    c.execute(DB.UPDATE_SCHEMA_VERSION + str(DB.SCHEMA_VERSION))
    conn.commit()


# Parses history command.
# This parse the output of `HISTTIMEFORMAT= history 1`
# Format: optional_whitespace + required_sequence_number + required_whitespace + command
def parse_history(history):
    match = re.search(r'^\s*(\d+)\s+(.*)$', history, re.MULTILINE and re.DOTALL)
    if match:
        sequence, cmd = int(match.group(1)), match.group(2)
        # log command discards if the command being logged has a suffix like "my_cmd <ts>"
        # If a user copy-pastes recent output, having this timestamp will look weird.
        copied_from_recent = \
            re.search(r'^(.*)\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$', cmd)
        if copied_from_recent:
            cmd = copied_from_recent.group(1)
        return sequence, cmd
    else:
        return None, None

def parse_date(date_format):
    if re.match(r'^\d{4}$', date_format):
        return 'strftime(\'%Y\', command_dt) = ?'
    if re.match(r'^\d{4}-\d{2}$', date_format):
        return 'strftime(\'%Y-%m\', command_dt) = ?'
    if re.match(r'^\d{4}-\d{2}-\d{2}$', date_format):
        return 'date(command_dt) = ?'
    else:
        print("Invalid date passed to -d")
        sys.exit(1)

def create_connection():
    conn = sqlite3.connect(recent_db, uri=recent_db.startswith("file:"))
    build_schema(conn)
    return conn


def build_schema(conn):
    try:
        c = conn.cursor()
        current = c.execute(DB.GET_SCHEMA_VERSION).fetchone()[0]
        if current != DB.SCHEMA_VERSION:
            migrate(current, conn)
    except (sqlite3.OperationalError, TypeError):
        migrate(0, conn)


def envvars_to_log():
    envvar_whitelist = {k.strip() for k in os.getenv('RECENT_ENV_VARS', '').split(',') if k.strip()}

    def is_var_interesting(name: str):
        # Anything starting with RECENT_ is welcome.
        if name.startswith("RECENT_"):
            return True
        for interesting_var in envvar_whitelist:
            # if name matches glob(interesting_var) then we will store it.
            # E.g - CONDA_* => we are interested in all env vars that start with CONDA_.
            if Path(name).match(interesting_var):
                return True
        return False

    return {k: v for k, v in os.environ.items() if is_var_interesting(k)}


# Entry point to recent-log command.
def log(args_for_test=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('-r',
                        '--return_value',
                        help='Command return value. Set to $?',
                        default=0,
                        type=int)
    parser.add_argument('-c', '--command', help='Set to $(HISTTIMEFORMAT= history 1)', default='')
    parser.add_argument('-p', '--pid', help='Shell pid. Set to $$', default=0, type=int)
    args = parser.parse_args(args_for_test)

    sequence, command = parse_history(args.command)
    pid, return_value = args.pid, args.return_value
    pwd = os.getenv('PWD', '')

    if not sequence or not command:
        print(Term.WARNING + ('recent: cannot parse command output, please check your bash '
                              'trigger looks like this:') + Term.ENDC)
        exit("""export PROMPT_COMMAND='{}'""".format(EXPECTED_PROMPT))
    log_command(command=command, pid=pid, sequence=sequence, return_value=return_value, pwd=pwd)


def log_command(command, pid, sequence, return_value, pwd):
    conn = create_connection()
    session = Session(pid, sequence)
    session.update(conn)

    if not session.empty:
        c = conn.cursor()
        json_data = "json('{}')".format(json.dumps({'env': envvars_to_log()}))
        # We pass current time instead of using 'now' in sql to mock this value.
        c.execute(DB.INSERT_ROW.format(json_data),
                  [int(time.time()), command, pid, return_value, pwd, session.id])

    conn.commit()
    conn.close()


# Imports bash_history into RECENT_DB
# Entry point to recent-import-bash-history command.
def import_bash_history_entry_point(args_for_test=None):
    description = ('recent-import-bash-history imports bash_history into ~/.recent.db. '
                   'Run `recent -h` for info about recent command.')
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-f',
                        help='Force import bash history ignoring previous imports',
                        action='store_true')
    args = parser.parse_args(args_for_test)
    import_marker = Path(
        os.environ.get("RECENT_TEST_IMPORT_FILE", "~/.recent_imported_bash_history"))
    import_marker = import_marker.expanduser().absolute()
    print(import_marker)
    if not args.f and import_marker.exists():
        print(Term.FAIL +
              'recent-import-bash-history failed: Bash history already imported into ~/.recent.db')
        print('Run the command with -f option if you are absolutely sure.' + Term.ENDC)
        parser.print_help()
        sys.exit(1)
    import_bash_history()
    import_marker.touch()


def import_bash_history():
    # Construct history from bash_history.
    # Example bash_history. The history has 3 entries. First entry has no timestamp attached to it.
    # The next 2 entries have timestamp attached to them. The last entry has some unknown comment
    # which we will ignore.
    """
    ls /
    #1571012545
    echo foo
    #1571012560
    #useless comment that should be ignored.
    cat bar
    """
    history = []
    # Phase 1 starts: After this phase history will be like this
    # [(-1, "ls /"), # This entry has no timestamp.
    #  (1571012545, "echo foo"),
    #  (1571012560, "cat bar")]
    last_ts = -1
    histfile = Path(os.environ.get("HISTFILE", "~/.bash_history")).expanduser()
    if not histfile.exists():
        return
    for line in histfile.read_text().splitlines():
        if not line:
            continue
        if line[0] == '#':
            try:
                last_ts = int(line[1:].strip())
            except Exception:
                # Ignore the exception.
                pass
            continue
        history.append([last_ts, line.strip()])

    # Phase 2 starts: After this phase history will be like this
    # [(1571012545, "ls /"), # Timestamp for this comes from its next entry
    #  (1571012545, "echo foo"),
    #  (1571012560, "cat bar")]
    last_ts = -1
    for i in range(len(history) - 1, -1, -1):
        if history[i][0] == -1 and last_ts != -1:
            history[i][0] = last_ts
        elif history[i][0] != -1 and last_ts == -1:
            last_ts = history[i][0]
    # Add the history entries into recent's DB.
    conn = create_connection()
    import random
    # Create a session with a random -ve pid and random -ve sequence id.
    pid = -random.randint(1, 10000000)
    session = Session(pid=pid, sequence=-random.randint(1, 10000000))
    session.update(conn)
    for cmd_ts, cmd in history:
        c = conn.cursor()
        c.execute(DB.INSERT_ROW_NO_JSON, [
            cmd_ts, cmd, pid,
            # exit status=-1, working directory=/unknown
            -1, "/unknown", session.id])  # yapf: disable
    conn.commit()
    conn.close()

    
# Returns a list of queries to run for the given args
# Return type: List(Pair(query, List(query_string)))
def query_builder(args):
    # TODO
    #https://docs.python.org/3/library/argparse.html#mutual-exclusion
    if args.r and args.s:
        sys.exit(Term.FAIL + 'Only one of -re and -sql should be set' + Term.ENDC)
    sum_status = sum(1 for x in [args.failure, args.code != -1] if x)
    if sum_status > 1:
        sys.exit(Term.FAIL + ('Only one of --failure and --code has to be set') + Term.ENDC)
        
    query = DB.TAIL_N_ROWS_TEMPLATE_DEDUP if args.dedup else DB.TAIL_N_ROWS_TEMPLATE
    filters = []
    parameters = []
    if args.session:
        filters.append('session = ?')
        parameters.append(Session.session_id_string())
    if args.failure:
        #https://stackoverflow.com/questions/16749121/what-does-mean-in-python
        filters.append('return_val != 0')
    if args.code != -1:
        filters.append('return_val == ?')
        parameters.append(args.code)
    if not args.self:
        # Dont return recent commands unless user asks for it.
        filters.append("""command not like 'recent%'""")
    if args.pattern:
        if args.r:
            filters.append('command REGEXP ?')
            parameters.append(args.pattern)
        elif args.s:
            filters.append(args.pattern)
        else:
            filters.append('command like ?')
            parameters.append('%' + args.pattern + '%')
    if args.d:
        filters.append('pwd = ?')
        parameters.append(str(Path(args.d).expanduser().absolute()))
    if args.D:
        filters.append(parse_date(args.D))
        parameters.append(args.D)
    for env_var in args.env:
        split = env_var.split(":")
        if len(split) == 1:
            filters.append('json_extract(json_data, "$.env.{}") is not null'.format(split[0]))
        else:
            filters.append('json_extract(json_data, "$.env.{}") = ?'.format(split[0]))
            parameters.append(split[1])
    filters.append('length(command) <= {}'.format(args.limit))
    try:
        n = int(args.n)
        parameters.append(n)
    except:
        exit(Term.FAIL + '-n must be a integer' + Term.ENDC)
    where = 'where ' + ' and '.join(filters) if len(filters) > 0 else ''

    ret = []
    if not args.insensitive:
        # No params required for case on query.
        ret.append((DB.CASE_ON, []))
    query_and_params = query.replace('where', where), parameters
    ret.append(query_and_params)
    return ret


# Returns true if `item` matches `expr`. Used as sqlite UDF.
def regexp(expr, item):
    reg = re.compile(expr)
    return reg.search(item) is not None


def make_arg_parser_for_recent():
    description = ('recent is a convenient way to query bash history. '
                   'Visit {} for more examples or to ask questions or to report issues'
                   ).format(Term.UNDERLINE + 'https://github.com/dotslash/recent2' + Term.ENDC)
    epilog = 'To import bash history into recent db run {}'.format(Term.UNDERLINE +
                                                                   'recent-import-bash-history' +
                                                                   Term.ENDC)
    parser = argparse.ArgumentParser(description=description, epilog=epilog)
    parser.add_argument('pattern',
                        nargs='?', 
                        default='', 
                        help='optional pattern to search')
    
    parser.add_argument('-n', 
                        metavar='NUM',
                        help='max results to return', 
                        default=20)

    # Filters for command success/failure.
    parser.add_argument('-e',
                        '--code',
                        metavar='CODE',
                        help='int exit status of the commands to return. -1 => return all.',
                        default=-1)
    parser.add_argument('-f',
                        '--failure',
                        help='only return commands that exited with failure',
                        action='store_true')
    # Other filters/options.
    parser.add_argument('-d',
                        metavar='DIR',
                        help='The Directory where the command runs', 
                        default='')
    parser.add_argument('-N',
                        '--session',
                        help='Returns commands only from current session',
                        action='store_true')
    parser.add_argument('-D',
                        metavar='DATE',
                        help='date in YYYY, YYYY-MM, or YYYY-MM-DD formats',
                        default='')
    parser.add_argument('-S',
                        '--self',
                        help='Return `recent` commands also in the output',
                        action='store_true')
    parser.add_argument('-L',
                        '--limit',
                        help='Ignore commands longer than this.',
                        default=400)
    parser.add_argument('-E',
                        '--env',
                        action='append',
                        help=('Filter by shell env vars. Env vars set in RECENT_ENV_VARS '
                              'as comma separated list will be captured.'),
                        metavar='key[:val]',
                        default=[])
    parser.add_argument('-p',
                        '--dedup', 
                        action='store_true', 
                        help=('Deduplication'))

    # CONTROL OUTPUT FORMAT
    # Hide time. This makes copy-pasting simpler.
    parser.add_argument('-t',
                        '--time',
                        help='Displays the local time converted from UTC',
                        action='store_true')
    parser.add_argument('-g',
                        '--debug', 
                        help='Debug mode', 
                        action='store_true')
    parser.add_argument('-l',
                        '--detail',  
                        help='Return detailed output', 
                        action='store_true')
    parser.add_argument(
        '-c',
        '--columns',
        metavar='COL',
        help=('Comma separated columns to print if --detail is passed. Valid columns are '
              'command_dt,command,pid,return_val,pwd,session,json_data'),
        default="command_dt,command,json_data")

    # Query type - regex/sql.
    parser.add_argument('-r',
                        help='enable regex search pattern', 
                        action='store_true')
    parser.add_argument('-s',
                        help='enable sqlite search pattern', 
                        action='store_true')
    parser.add_argument('-i',
                        '--insensitive',
                        help='ignore case distinctions in patterns and data',
                        action='store_true')
    # https://github.com/awaxa/recent2/commit/eeead062675b89489674654e4b5c8def467a2e08                   
    argcomplete.autocomplete(parser)                    
    return parser


def check_prompt(debug):
    if os.environ.get('RECENT_CUSTOM_PROMPT'):
        if debug:
            print("RECENT_CUSTOM_PROMPT is set. Not checking prompt")
        return
    actual_prompt = os.environ.get('PROMPT_COMMAND', '')
    export_prompt_cmd = '''export PROMPT_COMMAND='{}' '''.format(EXPECTED_PROMPT)
    if EXPECTED_PROMPT not in actual_prompt:
        print(Term.BOLD + "PROMPT_COMMAND env variable is not set. " +
              "Add the following line to .bashrc or .bash_profile" + Term.ENDC)
        sys.exit(Term.UNDERLINE + export_prompt_cmd + Term.ENDC)


def tty_width():
    import shutil
    sz = shutil.get_terminal_size(fallback=(0, 0))
    return sz.columns


def pad(raw_text, print_text):
    allowed_width = min(tty_width() - 30, 50)
    to_pad = max(allowed_width - len(raw_text), 0)
    return print_text + (' ' * to_pad)


def handle_recent_command(args):
    check_prompt(args.debug)  # Fail the command if PROMPT_COMMAND is not set
    conn = create_connection()
    # Install REGEXP sqlite UDF.
    conn.create_function("REGEXP", 2, regexp)
    # Register the queries executed. (Replace new lines with spaces in the query)
    queries_executed = []

    def update_queries_executed(inp):
        if inp == DB.GET_COMMANDS_TABLE_SCHEMA:
            return
        trans = inp.replace('\n', ' ')
        queries_executed.append(trans)

    conn.set_trace_callback(update_queries_executed)
    c = conn.cursor()
    detail_results = []
    columns_to_print = set(args.columns.split(','))
    columns_to_print.update(['command_dt', 'command', 'return_val'])
    for query, parameters in query_builder(args):
        for row in c.execute(query, parameters):
            query_columns = DB.TAIL_N_ROWS_DEDUP_COLUMNS if args.dedup else DB.TAIL_N_ROWS_COLUMNS
            row_dict = {
                query_columns[i]: row[i]
                for i in range(len(row))
                if query_columns[i] in columns_to_print
            }
            if 'command_dt' not in row_dict or 'command' not in row_dict:
                # Why would we have these entries?
                continue
            if args.detail:
                detail_results.append(row_dict)
                continue
            colored_cmd = row_dict['command']
            if row_dict.get('return_val', 0) > 0:
                # Show failed commands in red.
                # We do > 0 because for commands we got via import_bash_history, the return_val
                # is negative
                colored_cmd = Term.FAIL + colored_cmd + Term.ENDC
                
            if args.time:
                #https://www.sqlite.org/lang_datefunc.html
                #https://groups.google.com/g/comp.lang.python/c/PhtX3V0jsSA/m/7cSdd0y7BQAJ
                #https://stackoverflow.com/questions/4770297/convert-utc-datetime-string-to-local-datetime
                from_zone = tz.tzutc()
                to_zone = tz.tzlocal()
                cmd_time = datetime.strptime(row_dict["command_dt"], '%Y-%m-%d %H:%M:%S').replace(tzinfo=from_zone).astimezone(to_zone).strftime("%Y-%m-%d %H:%M:%S")
                print(f'{Term.YELLOW}{cmd_time}{Term.ENDC} {colored_cmd}')
            else:
                print(colored_cmd) 

            
    if args.detail:
        if 'json_data' not in columns_to_print:
            print(tabulate(detail_results, headers="keys"))
        else:
            for res in detail_results:
                for k, v in res.items():
                    print(Term.BOLD + Term.OKBLUE + k + Term.ENDC + ": " + str(v))
                print("---------------------------------")

    if args.debug:
        schema = None
        for row in c.execute(DB.GET_COMMANDS_TABLE_SCHEMA, []):
            schema = row[0]
        print("=========DEBUG=========")
        print("---SCHEMA---")
        print(schema)
        print("---QUERIES---")
        print("To reproduce this output run the following sqlite command")
        print("""sqlite3 {} "{}" """.format(recent_db, '; '.join(queries_executed)))
    conn.close()


def main():
    parser = make_arg_parser_for_recent()
    args = parser.parse_args()
    handle_recent_command(args)


if __name__ == '__main__':
    print("=================")
    print("Executing recent from __main__.")
    print("This means recent2 is being run via `python recent2.py`")
    print("=================")
    main()
