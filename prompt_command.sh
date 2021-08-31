#/bin/bash

#https://github.com/dotslash/recent2/issues/32
export RECENT_CUSTOM_PROMPT=1
recent_prompt_command() {
  #https://bleepcoder.com/bash-git-prompt/259531236/iterm2-shell-integration-breaks-git-prompt-command-fail
  #Replace `$?' with `$__bp_last_ret_value' with the help of bash preexec hooks:
  # https://github.com/rcaloras/bash-preexec
  log-recent -r $__bp_last_ret_value -c "$(HISTTIMEFORMAT= history 1)" -p $$
}


#https://github.com/cantino/mcfly/blob/fd269640f290ce3344cf5800e16d0e8729e0ff43/mcfly.bash#L59
if [ -z "$PROMPT_COMMAND" ]
then
  PROMPT_COMMAND="recent_prompt_command"
else
  PROMPT_COMMAND="recent_prompt_command;${PROMPT_COMMAND#;}"
fi  
