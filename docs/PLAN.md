# callmaibe: implementation plan

Status: draft, under discussion. Nothing is implemented yet.

## Goal

Let an AI coding agent phone you when it finishes a task or gets stuck. It
speaks a summary of what it did and what it deliberated on, listens to your
answer, and keeps a real back-and-forth voice conversation going. Everything
you say is returned to the agent as text so it can act on it. Calls are never
placed while you have them switched off.

Calls go over SIP (the open internet-telephony standard), so the project is
not tied to one app or vendor. The recommended setup is two free
`sip.linphone.org` accounts plus the Linphone app on your phone, but any SIP
provider and any softphone with push notifications should work.

## Non-goals for v1

- Real phone numbers (PSTN). Possible later through a SIP trunk adapter.
- Inbound calls (you call the agent). Planned for v2.
- Video, group calls, more than one callee.
- Running our own SIP server.

## Architecture

```
 Agent (Claude Code or any MCP client)
     │ stdio (MCP)
     ▼
 callmaibe mcp ── HTTP on 127.0.0.1 (token) ──►  callmaibe daemon
  (thin, one per agent session)                     ├─ Policy       on/off/manual, snooze, quiet hours
                                                    ├─ Conversation turns, timeouts, barge-in, hangup
                                                    ├─ TTS          kokoro | piper | edge-tts | (openai, elevenlabs)
                                                    ├─ STT + VAD    faster-whisper | (openai) + silero/webrtc VAD
                                                    └─ Telephony    pjsua2 | liblinphone
                                                           │ SIP over TLS, SRTP media
                                                           ▼
                                                    SIP provider (sip.linphone.org or any)
                                                           │ push notification + SIP
                                                           ▼
                                                    Softphone on your phone (Linphone iOS/Android)
```

Why a separate daemon instead of doing everything inside the MCP server:

- One SIP registration. Every agent session spawns its own MCP server
  process; they must not each register and call independently.
- One call at a time. The daemon owns the line and rejects a second
  `call_start` while a call is active.
- The STT model stays loaded. Loading Whisper takes several seconds; doing
  it per call adds dead air.
- A call outlives a single tool call. The line stays open between the
  agent's turns.
- CLI toggles (`callmaibe off`) work even with no agent running.

The MCP server starts the daemon automatically if it is not running (lock file
plus health check), so users never have to start it by hand.

## Call flow

1. Agent calls `call_start(message)`.
2. Daemon checks policy. If calls are blocked it returns immediately with
   `status: "unavailable"` and the reason (off, snoozed, quiet hours).
3. Daemon dials the configured callee and rings for up to `ring_timeout_s`.
   Outcomes: answered, declined, no answer, busy, error. A declined call
   triggers an automatic snooze (see Policy).
4. On answer: speak `message` with TTS, then listen. VAD detects speech; the
   turn ends after `end_silence_ms` of silence or `max_turn_s`. If you start
   talking while the agent is still speaking (barge-in), playback stops and
   the daemon listens.
5. STT transcribes your turn. The tool returns
   `status: "in_call", user_said: "..."`.
6. While the agent thinks about its next turn, the daemon plays a soft
   "thinking" cue so the line is not dead silent.
7. Agent calls `call_say(message)`. Steps 4 to 6 repeat for as many turns as
   needed.
8. Agent calls `call_end(message)` to say goodbye and hang up. The call also
   ends when you hang up (the next tool result says `ended_by_user` and
   includes your last words), after `idle_hangup_s` without a `call_say`, or
   after `max_call_min`.

Every tool result carries a `status` field so the agent can branch on it.

## MCP tools

| Tool | Arguments | Returns |
|---|---|---|
| `call_start` | `message`, optional `topic` | status, `user_said`, call id |
| `call_say` | `message`, `expect_reply` (default true) | status, `user_said` |
| `call_end` | optional `message` | status, transcript path |
| `call_status` | none | availability, mode, active call |

No tool accepts a phone number or SIP address. The callee comes only from
config, so a prompt injection cannot make the agent call someone else.

Tool calls block while ringing and listening, which can take a minute or two.
MCP clients need a long enough tool timeout (Claude Code: `MCP_TOOL_TIMEOUT`).
The server sends MCP progress notifications while it waits.

## Availability (the off switch)

Enforced in the daemon. Toggling it off actually prevents calls; it does not
depend on the agent obeying an instruction.

Calling is off by default. Only the user can switch it on, one chat session
at a time:

- `/call on`: this session's agent may call you when it judges it useful
  (long task done, blocked on a decision, approval needed).
- `/call off`: this session stops calling.
- `/call now`: call right now about the current work, even if calling is off
  for this session.

The switch has to come from the user, never from the agent, so there is no
MCP tool that turns calling on. `/call` is handled by code that only runs on
user input: either the command's own pre-run shell step or a
`UserPromptSubmit` hook. M3 checks which one Claude Code supports. That code
writes the session's state where the daemon reads it. A session where nobody
typed `/call on` cannot place calls. Mapping a hook's `session_id` to the
right MCP server process is the other M3 detail to verify (fallback: key on
the Claude Code process id, which is the parent of both).

Above the per-session switch sits a global kill switch: `callmaibe off`
blocks every session, `callmaibe on` lifts it.

Session identity (implemented in `session.py`): Claude Code passes hooks a
`session_id`, but gives MCP servers no session id (only
`CLAUDE_PROJECT_DIR`). So both the hook and the MCP server derive the key
from the nearest ancestor Claude Code process, `claude-<pid>` (with a
`node` fallback). `CALLMAIBE_SESSION` overrides it, and a plain terminal gets
`local`. Consequences:

- `claude --resume` or `--continue` starts a new process, so calling is off
  again until you type `/call on` in the resumed session.
- Two Claude Code windows are separate sessions, each with its own switch.
- PIDs can be reused after a reboot. A stale "on" entry can then match a new
  session, but entries older than 7 days are pruned, and the global switch
  and quiet hours still apply.

Overrides that block calls even in a session where calling is on:

- Snooze until a time (`callmaibe snooze 2h`).
- Quiet hours: weekly time ranges in config, for example work hours.
- Declined call: declining on your phone means "busy" and snoozes calls for
  `decline_snooze_min` (default 30). This is the remote off switch and needs
  no extra app.

Controls:

- CLI (global): `callmaibe on | off | snooze 2h | status`
- Claude Code: `/call on | off | now | status`
- Phone: decline the call.
- Later: text commands sent as SIP MESSAGE from the softphone's chat. This
  only works for plaintext chat, because Linphone's encrypted chat cannot be
  read by a third-party client.

When calls are unavailable, the agent is instructed to put its summary in the
terminal instead.

## When the agent calls

Distributed as a Claude Code plugin containing:

- the MCP server config,
- a `/call` command,
- a skill describing when to call (a long task finished, blocked on a
  decision, approval needed before a risky step) and how to talk on the
  phone: short sentences, no code, markdown or URLs, one question at a time,
  read decisions back before acting on them.

For other MCP clients, the README gives a snippet for their instructions file.

An optional Stop hook ("always call when the agent finishes") is planned for
v1.1, off by default.

## Telephony backend

A small interface so backends can be swapped:

```python
class Telephony(Protocol):
    def start(self) -> None: ...                       # init + register
    def dial(self, uri: str) -> "Call": ...

class Call(Protocol):
    state: CallState                                   # ringing, active, ended(reason)
    def play(self, pcm: Iterable[bytes]) -> None: ...  # 16 kHz mono s16le frames
    def stop_playback(self) -> None: ...
    def frames(self) -> Iterator[bytes]: ...           # incoming audio, same format
    def hangup(self) -> None: ...
```

Candidates, to be decided by the spike (M0):

| | pjsua2 (PJSIP) | liblinphone (pylinphone) |
|---|---|---|
| Install | community wheels (`pjsua2-python`, `pjsua2-wheel`), Win/macOS/Linux | official Python wrapper; install path to verify |
| Python | wheels listed for 3.8 to 3.12 | to verify |
| Audio access | frame-level via `AudioMediaPort`, so streaming and barge-in are easy | mainly file player and recorder, so streaming is hackier |
| Fit with the Linphone app | standard SIP, SRTP, ICE; no ZRTP | same stack as the app: ZRTP, ICE and Opus all match |
| License | GPLv2+ or commercial | AGPLv3 or commercial |

Leaning toward pjsua2 for the streaming audio API, with liblinphone as the
fallback if pjsua2 cannot hold a call with the Linphone app reliably.

- Encryption: SIP over TLS plus SRTP (SDES) is required, not optional. The
  Linphone apps' defaults for sip.linphone.org accounts set
  `media_encryption=srtp` with `media_encryption_mandatory=1`, and
  `transport=tls`, so an unencrypted offer gets rejected (488). SDES keys
  travel in the SDP, which is why TLS is needed. ZRTP only with liblinphone.
  A `none` setting exists for debugging against other providers.
- Codecs: Opus if both ends support it, otherwise G.711 (PCMU/PCMA, 8 kHz).
  The internal pipeline runs at 16 kHz mono and resamples at the edges.
- NAT (investigated, see below): use ICE with STUN by default, optional TURN
  in config. The docs will cover the classic symptom: the call connects but
  there is no audio.

### Platform findings (M0, 2026-10-17)

- The pjsua2 Windows wheels (`pjsua2-python` 2.15.1.post1 and
  `pjsua2-wheel` 2.15.3, both cp312 only) are built without TLS
  (`PJSIP_EUNSUPTRANSPORT`) and without Opus, so they cannot reach
  linphone.org accounts.
- The `pjsua2-python` Linux wheel has TLS (bundled OpenSSL 1.0.2k), SRTP,
  ICE, TURN and `AudioMediaPort`. Codecs: G.722 (16 kHz), PCMU/PCMA, no
  Opus. A TLS REGISTER to sip.linphone.org got a 403 for a fake user, so
  the handshake works.
- The official liblinphone Python wheels exist only for Linux x86_64, cp312.

Decision: the SIP backend runs on Linux: natively, in WSL2 on Windows
(mirrored networking recommended), or in Docker (the macOS route for now).
Everything else, including the `local` mic/speaker backend, runs on any OS.
The Windows-side MCP server, CLI and hooks reach a daemon in WSL over
localhost, and `CALLMAIBE_HOME` points both sides at one shared config and
state directory. Later: build our own pjsua2 wheels with OpenSSL 3 and Opus
for all three OSes, since OpenSSL 1.0.2 is end of life.

### Platform matrix and installers

| OS | MCP server, CLI, hooks | SIP daemon (pjsua2) | `local` backend |
|---|---|---|---|
| Linux x86_64 | native | native (wheel bundles OpenSSL 1.0.2) | native |
| macOS (arm64 14+, x86_64 15+) | native | native; the wheel bundles OpenSSL 3 (not yet tested) | native |
| Windows | native | inside WSL2 | native |

`runtime.py` decides at spawn time (`[daemon] runner = "auto" | "native" |
"wsl"`). In auto mode, Windows with `media.backend = "pjsua2"` runs the
daemon through `wsl.exe` with the shared `CALLMAIBE_HOME`; passwords cross
via `WSLENV`. Verified on the dev machine.

Installers, one per platform family, both ending in `callmaibe setup`:

- `scripts/install.sh` (Linux, macOS): ensure `uv`, then
  `uv tool install --python 3.12 "callmaibe[sip,speech]"`.
- `scripts/install.ps1` (Windows):
  1. Ensure `uv`, then install the Windows side
     (`callmaibe[speech,local]`, so the local mode works too).
  2. Check WSL2 and a distro; if missing, print `wsl --install -d Debian`
     and stop.
  3. Inside WSL, without sudo: fetch the `uv` binary into `~/.local/bin`,
     create `~/.venvs/callmaibe` with Python 3.12, and install
     `callmaibe[sip,speech]`.
  4. Offer to set `networkingMode=mirrored` in `.wslconfig` (asks first).

### NAT findings

The hard case is a PC behind a home router calling a phone on mobile data,
where carrier-grade NAT is often symmetric. STUN alone fails for a
port-restricted home NAT talking to a symmetric NAT, so media needs a relay
somewhere.

- **No TURN for free accounts.** The NAT policy the Linphone apps ship for
  sip.linphone.org accounts (`assistant_linphone_default_values` in
  linphone-iphone and linphone-android) is `stun_server=stun.linphone.org`,
  `protocols=stun,ice`. TURN is not configured, and no public TURN
  credentials are documented.
- **The server may relay media itself.** Flexisip's MediaRelay module is
  `enabled=true` by default. It rewrites SDP so RTP goes through the proxy
  and adds relay ICE candidates. With `force-relay-for-non-ice-targets=true`
  (also the default) it puts the relay address in the `c=` line even when
  ICE candidates are present. If sip.linphone.org runs it, NAT is solved
  server-side. Belledonne's pages say the service is powered by Flexisip
  with media relay, but nothing confirms that it is enabled for free
  accounts.
- **How to check, in one call.** Place a call and read the SDP that comes
  back in the 200 OK. A `c=` address or `typ relay` candidate belonging to
  Linphone's servers, rather than the phone's carrier, means the relay is
  on. This replaces guessing from "is there audio".
- **Fallback if there is no relay: TURN on the PC side only.** One relayed
  side is enough. TURN permissions are per IP address, not per port, so a
  symmetric NAT on the phone can still reach the PC's relay candidate. The
  phone needs no change, because the Linphone app already does ICE. TURN
  options: Cloudflare Realtime TURN (1,000 GB/month free, then $0.05/GB; a
  voice call uses well under 1 MB per minute) or self-hosted coturn on a
  VPS.
- **IPv6.** The iOS app defaults to `use_ipv6=1`, and some mobile networks
  are IPv6-only (with CLAT for IPv4). ICE with both address families
  covers this; it belongs in the test matrix.

## Speech

TTS interface: `synthesize(text) -> pcm16k`. Output is streamed sentence by
sentence: sentence 1 plays while sentence 2 is synthesized.

- `kokoro` (kokoro-onnx): proposed default. Local, free, natural voices,
  Apache-2.0. Measured on the dev laptop's CPU: real-time factor about 0.5
  for fp32 (310 MB) and 0.45 for int8 (88 MB), model load about 2.5 s. With
  sentence streaming, the first audio arrives roughly 1.5 to 2.5 s after
  the agent's message.
- `piper`: offline and lighter, more robotic.
- `edge-tts`: opt-in. Natural voices, needs internet, sends text to
  Microsoft, and uses an unofficial endpoint that may break.
- `openai`, `elevenlabs`: optional paid extras.

STT interface: `transcribe(pcm) -> text`.

- `faster-whisper`: default, runs locally on CPU (int8). The model size is
  configurable; uses the GPU if one is available.
- `openai`: optional API.

VAD: silero-vad (more accurate) or webrtcvad (smaller).

Latency targets: under 1.5 s from the end of your speech to the tool result,
and about 2 s to the first TTS audio on CPU with Kokoro.

## Config

Stored in the platform user config dir (`%LOCALAPPDATA%\callmaibe\config.toml`,
`~/.config/callmaibe/config.toml`, `~/Library/Application Support/...`).
Secrets come from environment variables or the OS keyring, never from files
in the repo.

```toml
[account]                     # the account the agent calls from
uri = "sip:my-agent@sip.linphone.org"
password_env = "CALLMAIBE_SIP_PASSWORD"
transport = "tls"
stun = "stun.linphone.org"
# Only needed if the provider does not relay media (see NAT findings):
# turn = { server = "turn.cloudflare.com:3478", username = "...", password_env = "CALLMAIBE_TURN_PASSWORD" }

[callee]
uri = "sip:me@sip.linphone.org"

[media]
backend = "pjsua2"
encryption = "srtp"           # srtp | zrtp | none

[tts]
engine = "kokoro"
voice = "af_heart"

[stt]
engine = "faster-whisper"
model = "small"
language = "en"

[conversation]
ring_timeout_s = 45
end_silence_ms = 1200
max_turn_s = 60
idle_hangup_s = 120
max_call_min = 20

[policy]
enabled = true                # global kill switch; sessions still need /call on
decline_snooze_min = 30
timezone = "local"
quiet_hours = [
  { days = "mon-fri", from = "09:00", to = "17:00" },
]
```

A `callmaibe setup` wizard asks for the accounts and callee, tests
registration, and places a test call.

## CLI

```
callmaibe setup              interactive config + test call
callmaibe daemon             run in foreground (normally auto-started)
callmaibe test-call          ring, speak, echo back what you said
callmaibe say "text"         one-way call: speak and hang up (for hooks/scripts)
callmaibe on|off             global kill switch
callmaibe snooze 2h          block calls for a while
callmaibe status             mode, snooze, quiet hours, registration, active call
callmaibe logs | transcripts
```

## Security and privacy

- The callee comes from config only; no tool can dial an arbitrary address.
- All inbound calls are rejected in v1.
- The daemon API binds to 127.0.0.1 and requires a random token stored in a
  file only the user can read.
- Transcripts are stored locally, with configurable retention or turned off.
  Audio is not stored by default.
- Whoever answers your phone can steer the agent. A spoken "yes" may approve
  any action, including destructive ones, but the skill makes the agent read
  the exact action back and hear a clear yes before doing it.
- `edge-tts` and cloud STT send text and audio to third parties. This is
  documented, and local alternatives exist.

## Repository layout

```
callmaibe/
├─ pyproject.toml
├─ README.md
├─ LICENSE
├─ docs/
│  ├─ PLAN.md
│  ├─ setup-linphone.md          accounts, phone app, push, screenshots
│  └─ troubleshooting.md         NAT/no audio, encryption mismatch, timeouts
├─ src/callmaibe/
│  ├─ cli.py
│  ├─ config.py
│  ├─ policy.py
│  ├─ daemon.py                  local HTTP API, owns the line
│  ├─ conversation.py            turn engine
│  ├─ mcp_server.py
│  ├─ telephony/{base,pjsua2,linphone}.py
│  ├─ tts/{base,edge,piper}.py
│  ├─ stt/{base,faster_whisper}.py
│  └─ audio/{vad,resample,cues}.py
├─ plugin/                       Claude Code plugin
│  ├─ .claude-plugin/plugin.json
│  ├─ .mcp.json
│  ├─ commands/call.md
│  └─ skills/phone-call/SKILL.md
└─ tests/                        policy, conversation engine with a fake telephony backend
```

## Requirements

For users:

- Python 3.12 (the pjsua2 wheels' current limit; `uv` installs it
  alongside other versions).
- For real phone calls, Linux for the SIP daemon: native, WSL2 on Windows,
  or Docker. See "Platform findings".
- Two SIP accounts: one for the agent, one for you. Free at
  `subscribe.linphone.org`, or any SIP provider.
- A softphone on your phone that is logged into your account with
  notifications allowed. Linphone (iOS/Android) uses push plus CallKit, so it
  rings even when closed.
- An MCP client. Claude Code gets the plugin; others use manual config.
- Disk: around 150 to 500 MB for the Whisper model, depending on size.

For development: `uv`, `ruff`, `pytest`, and GitHub Actions CI (lint plus
unit tests on all three OSes; telephony is mocked in CI).

## Milestones

**M0: spike (go/no-go).** Register the agent account with pjsua2 and with
liblinphone, call the phone, play a WAV, record a WAV. Test matrix:

- the pjsua2 wheel is built with TLS and SRTP (OpenSSL); without them it
  cannot talk to linphone.org accounts at all,
- phone on the same Wi-Fi,
- phone on mobile data, recording whether the 200 OK SDP shows a Linphone
  media relay address (decides whether TURN is needed),
- the same with TURN forced on the PC side, as the fallback path,
- Linphone app killed (push wake-up),
- encryption settings against the Linphone app's defaults (mandatory SRTP).

Exit when one backend passes all of them.

**M1: daemon core.** Config, telephony backend, TTS/STT/VAD pipeline.
`callmaibe test-call` holds a full spoken echo conversation.

**M2: MCP and the conversation engine.** Tools, multi-turn calls, timeouts,
hangup detection, the thinking cue.

**M3: policy and plugin.** Modes, snooze, quiet hours, decline-snooze, CLI,
`/call`, the skill.

**M4: polish and release.** Setup wizard, barge-in, sentence-streamed TTS,
transcripts, docs, CI, PyPI.

Later: inbound calls (call your agent to resume a session), PSTN through a
SIP trunk or Twilio adapter, plugins for other agents, SIP MESSAGE commands,
a Docker image.

## Decisions

- License: GPLv3, the simplest fit with pjsua2 (GPLv2+) and liblinphone
  (AGPLv3).
- End of turn: silence only, no spoken keyword.
- Voice approval: a spoken yes can approve destructive actions, after the
  agent reads the action back.
- Calling: off by default, switched on per chat session by the user with
  `/call on`.
- STT: local faster-whisper.

## Open questions

1. Name: something with "ai" in it, since the tool is not tied to one agent.
2. TTS default: Kokoro sampled (fp32 vs int8, four voices); waiting on a
   listen.
