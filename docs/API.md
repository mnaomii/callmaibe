# Daemon HTTP API

Local only: binds `daemon.host:daemon.port` (default `127.0.0.1:47823`).
Every request needs header `Authorization: Bearer <token>`, where the token
is the content of `paths.token_file()` (created by the daemon on start with
user-only permissions). JSON in, JSON out. Long calls (`/call/*`) block until
the turn finishes, which can take minutes; clients use no read timeout (or
`ring_timeout_s + listen_timeout_s + max_turn_s + 60`).

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/health` | — | `{"ok": true, "version", "registered", "backend"}` (no auth needed) |
| GET | `/status?session=<key>` | — | `{"allowed": bool, "reason", "global_enabled", "session_enabled", "snoozed_until", "quiet_now", "active_call": {...} or null}` |
| POST | `/call/start` | `{"session", "message", "topic"?}` | TurnResult |
| POST | `/call/say` | `{"session", "call_id", "message", "expect_reply"?}` | TurnResult |
| POST | `/call/end` | `{"session", "call_id", "message"?}` | TurnResult |
| POST | `/shutdown` | — | `{"ok": true}` |

TurnResult is `status.TurnResult.to_dict()`:
`{"status", "user_said", "call_id", "reason", "transcript_path"}`.

Rules:

- `/call/start` checks `Policy.check(session)` first. If not allowed it
  returns `status: "unavailable"` with the reason and never dials. A
  `/call now` one-shot permit is consumed by a successful `/call/start`.
- After a `declined` result the daemon calls `Policy.on_declined()`.
- `/call/say` and `/call/end` are not policy-checked (the call is already up),
  but `session` must match the session that started the call.
- Policy toggles (`/call on|off|now`, `callmaibe on|off|snooze`) do not go
  through the daemon: they edit `policy.json` via `Policy`, so they work
  while the daemon is down. The daemon re-reads policy on every check.
