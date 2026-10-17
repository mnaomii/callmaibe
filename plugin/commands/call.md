---
description: Turn phone calls from the agent on or off for this session (on | off | now | status)
argument-hint: on | off | now | status
disable-model-invocation: true
allowed-tools: Bash(callmaibe session *)
---
!`callmaibe session $ARGUMENTS`

The user typed /call. The text above is the result. Repeat it to the user in one short sentence.
If it says a call is permitted now, call the user with call_start about the current work,
following the phone-call skill. Never run `callmaibe session` yourself.
