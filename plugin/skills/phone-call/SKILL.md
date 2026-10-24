---
name: phone-call
description: When and how to phone the user with the callmaibe tools (call_start, call_say, call_end). Use when a long task finishes, when blocked on a decision, or when approval is needed for a risky step.
---

# Phoning the user

Calling is off until the user types `/call on` in this session. Only the user can turn it on;
you cannot, so never try. If calling is on (or the user typed `/call now`), phone when:

- a long task has finished,
- you are blocked and need a decision,
- you need approval before a risky step.

Otherwise do not call. Do not call for small updates.

## How to run the call

1. `call_start`: open with a short summary of what you did and what you deliberated on and why.
   Then ask ONE question.
2. Listen to `user_said`, then reply with `call_say`. One question at a time.
   The phone keeps listening while you work. If they spoke meanwhile, your next message is
   not said: you get `in_call` with what they said and a `reason` saying so. Answer that
   first, then repeat what still matters.
3. Finish with `call_end` and a short goodbye. Always end calls you started.
   If `call_end` returns `in_call`, the person talked over your goodbye.
   The call is still up: answer `user_said` with `call_say`, then end again.

## How to speak

- Short plain sentences, as if talking. No markdown, code, file paths or URLs.
- Say numbers naturally ("about forty minutes", not "40m").
- Before any destructive action (deleting, overwriting, force pushing, spending money),
  read the exact action back and get a clear spoken yes. Anything unclear is a no.

## Handling results

- `unavailable` (or declined, no_answer, busy, line_busy): do NOT retry. Write the summary in
  the terminal instead.
- `ended_by_user`: the person hung up. Act on what was decided in `user_said`.
- `no_reply`: ask once more, then end the call politely.
- `not_found` or `error`: stop calling and write the summary in the terminal.
