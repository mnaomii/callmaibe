# callmaibe

Let your AI coding agent phone you. When it finishes a long task or gets stuck, it calls your phone over SIP, tells you what it did and what it weighed up, and has a spoken back-and-forth with you. Everything you say comes back to the agent as text, so it can act on it. Calls only happen while you have switched them on.

Example call (the agent speaks first):

```
Agent: Hi, it's Claude. I finished migrating the billing tests to pytest. Forty-one pass,
       two fail. I think both are real bugs, not test problems. Want me to fix them now?
You:   Yes, but don't touch the invoice module.
Agent: Understood, fix the two failing tests and leave the invoice module alone. Is that right?
You:   Yes.
Agent: Great. I'll start now and write the result in the terminal. Bye.
```

Status: early. Real SIP calls are not yet verified end to end, see [Project status](#project-status-and-roadmap).

## How it works

```
 Agent (Claude Code or any MCP client)
     | stdio (MCP)
     v
 callmaibe mcp  --- HTTP 127.0.0.1 + token --->  callmaibe daemon
 (one per agent session)                          policy | conversation | TTS | STT+VAD | SIP
                                                      | SIP over TLS, SRTP media
                                                      v
                                              SIP provider (sip.linphone.org or any)
                                                      | push notification + SIP
                                                      v
                                              Linphone on your phone
```

- The agent gets four MCP tools: `call_start`, `call_say`, `call_end`, `call_status`. None takes a phone number.
- A separate daemon owns the SIP registration, the single phone line and the loaded speech models. The MCP server starts it automatically.
- Speech is local by default: Kokoro for text-to-speech, faster-whisper for speech-to-text, a voice activity detector to find the end of your turn.
- Whether calls are allowed is decided in the daemon, not by the agent. You switch calling on per chat session with `/call on`.
- The agent runs on a computer; you answer on your phone. Linphone uses push notifications, so it rings even when the app is closed.
- On Windows the SIP daemon runs inside WSL2 (the Windows SIP wheels lack TLS). Everything else stays on Windows.

## Features

- Spoken multi-turn conversation: silence-based end of turn, a "thinking" sound while the agent works.
- Per-session on/off switch, a global kill switch, snooze, weekly quiet hours.
- Declining a call on your phone snoozes calling for a while (default 30 minutes).
- Claude Code plugin: `/call` command, a skill that teaches the agent when and how to phone.
- Works with any MCP client through manual config.
- Local transcripts, no audio stored.
- Local mic/speaker mode to try the conversation without any SIP account.
- SIP over TLS with SRTP, ICE/STUN, optional TURN.

## Requirements

- Two free SIP accounts: one for the agent, one for you (sip.linphone.org works).
- The [Linphone](https://linphone.org) app on your phone, logged into your account, notifications allowed.
- Python 3.12 (`uv` installs it for you).
- For real calls on Windows: WSL2 with a Linux distro (Debian recommended).
- About 150 to 500 MB of disk for the speech models, downloaded on first use.

## Installation

### Linux and macOS

```bash
git clone https://github.com/mnaomii/callmaibe && cd callmaibe
./scripts/install.sh
```

The script checks for `uv` (and asks before installing it), runs `uv tool install --python 3.12 --force "callmaibe[sip,speech,local] @ <source>"`, runs `uv tool update-shell`, and finishes by running `callmaibe setup` (skipped with `--yes`). No sudo. The default source is the GitHub archive of `main`; override it with `CALLMAIBE_SOURCE=/path/to/checkout ./scripts/install.sh` or `--source`. Add `--yes` to skip the questions.

macOS note: the SIP wheel there is untested. Use `--backend local` or Docker until that is verified.

### Windows

```powershell
git clone https://github.com/mnaomii/callmaibe; cd callmaibe
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1
```

Why WSL: the pjsua2 Windows wheels are built without TLS and Opus, so they cannot register with linphone.org. The SIP daemon therefore runs in WSL2 while the CLI, MCP server and hooks stay native Windows. The script:

1. installs `uv` if needed (asks first: winget or the official script),
2. installs the Windows side, `callmaibe[speech,local]`,
3. checks WSL and a distro; if missing it tells you to run `wsl --install -d Debian` (admin PowerShell, reboot) and stops. The Windows side is installed, so local mode already works,
4. inside WSL, without sudo: fetches the `uv` binary into `~/.local/bin`, creates `~/.venvs/callmaibe` and installs `callmaibe[sip,speech]`,
5. offers (asks first, makes a backup) to set `networkingMode=mirrored` in `%USERPROFILE%\.wslconfig`; then run `wsl --shutdown`,
6. runs `callmaibe setup` (see [Setup](#setup)), so installing and configuring is one step. With `-Yes` or `-DryRun` it only records `runner = "wsl"` and tells you to run the wizard yourself.

Options: `-Distro Debian`, `-Source <url-or-folder>` (or env var `CALLMAIBE_SOURCE`), `-Yes`, and `-DryRun` to print every command without running it.

### Manual install

```bash
uv tool install --python 3.12 "callmaibe[sip,speech,local] @ https://github.com/mnaomii/callmaibe/archive/refs/heads/main.zip"
uv tool update-shell
```

On Windows use `callmaibe[speech,local]` and set up the WSL side yourself: a Python 3.12 venv at `~/.venvs/callmaibe` with `callmaibe[sip,speech]`, then `callmaibe setup --runner wsl`. Extras: `speech` (Kokoro, faster-whisper, VAD, resampler), `sip` (pjsua2, Linux and Python 3.12 only), `local` (mic and speaker through sounddevice).

### Claude Code plugin

Inside Claude Code:

```
/plugin marketplace add mnaomii/callmaibe
/plugin install callmaibe@callmaibe-marketplace
```

`callmaibe setup` does this for you (see [Setup](#setup)). The plugin adds the MCP server (`callmaibe mcp`), the `/call` command, the hook that handles it and the phone-call skill. It needs the `callmaibe` command on your `PATH`, so install the package first. Raise Claude Code's MCP tool timeout, since a call blocks while ringing and listening: set `MCP_TOOL_TIMEOUT` (milliseconds, for example `600000`) in the environment that launches Claude Code. The wizard offers to set it under `env` in `~/.claude/settings.json`.

### Other MCP clients

`callmaibe setup` writes the config for Codex CLI, Claude Desktop, Cursor and Gemini CLI (see [Setup](#setup)). For any other client, add a stdio server to its MCP config, with the absolute path of `callmaibe` if it is not on the client's `PATH`, and a session name of your choice:

```json
{ "mcpServers": { "callmaibe": { "command": "callmaibe", "args": ["mcp"],
                                 "env": { "CALLMAIBE_SESSION": "NAME" } } } }
```

Then paste instructions like these into its instructions file (AGENTS.md or similar):

```
You can phone the user with call_start, call_say and call_end, but only if call_status says
calling is available; the user switches that on. Call when a long task finishes, when you are
blocked on a decision, or when you need approval for a risky step. Speak in short plain sentences,
one question at a time, no code or URLs. Read back any destructive action and get a clear yes
before doing it. If calling is unavailable, write the summary in the terminal instead.
```

Other clients have no `/call` hook. Switch calling on from a terminal with `callmaibe session on --session NAME`, using the same name as `CALLMAIBE_SESSION` in the client's MCP config. `callmaibe setup --clients other` prints this snippet and the instructions for you.

## Setup

1. **Create two accounts** at <https://subscribe.linphone.org>: one for you (the one that rings) and one for the agent. Each needs its own email address. Creating yours inside the Linphone app also works, but the app asks for a phone number.
2. **Log your phone into your account.** Install Linphone, choose the **Linphone account** login (not "third-party SIP account") and allow notifications. That login turns on push, so the phone rings even when the app is closed. Do not log the phone into the agent's account.
3. **Run the wizard.** The installer starts it for you; to run it again at any time:
   ```bash
   callmaibe setup
   ```
   It asks, in order:
   1. **Agent SIP address.** `my-agent`, `my-agent@host` or `sip:my-agent@host`; a bare name gets `@sip.linphone.org`.
   2. **Your SIP address** (the phone that rings), same forms; it must differ from the agent's.
   3. **Agent password**, typed hidden, twice. It goes into the OS keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service), never into a file and never onto the screen. If one is already stored you choose keep (default) or replace. With no keyring (headless, WSL) nothing is stored and it explains the `CALLMAIBE_SIP_PASSWORD` environment variable instead; that variable takes precedence over the keyring. Avoid `setx` on Windows: it stores the value in plaintext in the registry.
   4. **Backend.** `pjsua2` for real phone calls, `local` for your laptop mic and speaker. On Windows with `pjsua2` it sets the runner to `wsl`, checks that the WSL Python from `scripts\install.ps1` exists, and prints how the daemon will run.
   5. **Clients to install into.** Numbers separated by commas; Enter keeps the ones it found on this machine.

      | Client | Found when | What it does |
      |---|---|---|
      | Claude Code | `claude` is on `PATH` | Installs the plugin (`claude plugin marketplace add`, then `claude plugin install callmaibe@callmaibe-marketplace`, user scope). If that fails it falls back to `claude mcp add --scope user callmaibe`, and `/call` then needs the plugin. Offers to set `MCP_TOOL_TIMEOUT` to `600000` under `env` in `~/.claude/settings.json`. |
      | Codex CLI | `codex` on `PATH` or `~/.codex` | `codex mcp add callmaibe` (or appends `[mcp_servers.callmaibe]` to `~/.codex/config.toml`), and adds the agent instructions to `~/.codex/AGENTS.md` between `<!-- callmaibe:start -->` and `<!-- callmaibe:end -->`. |
      | Claude Desktop | its config folder exists | `mcpServers.callmaibe` in `claude_desktop_config.json`. |
      | Cursor | `~/.cursor` exists | `mcpServers.callmaibe` in `~/.cursor/mcp.json`. |
      | Gemini CLI | `gemini` on `PATH` or `~/.gemini` | `mcpServers.callmaibe` (with a 10 minute timeout) in `~/.gemini/settings.json`. |
      | Other | never preselected | Prints the JSON snippet and the instructions. |

      The MCP entries use the absolute path of `callmaibe`. Each file is backed up first as `<file>.bak-YYYYmmdd-HHMMSS`, only the callmaibe entry is changed, everything else is kept, running the wizard again changes nothing, and it prints exactly what it changed. Clients other than Claude Code have no `/call`; it prints the command to turn calling on for each, such as `callmaibe session on --session codex`.
   6. It writes `config.toml` (keeping any keys it does not manage), prints a summary, and, for `pjsua2` with a password available, asks `Place a test call now? Your phone will ring [y/N]` (default no).

   On Windows the WSL daemon has no keyring, so `callmaibe` reads the password on the Windows side and hands it only to the daemon process (via `WSLENV`), not to your whole environment. `callmaibe password set|status|delete` still works for changing the password later.
4. **Test it** (if the wizard did not already):
   ```bash
   callmaibe test-call
   ```
   Your phone rings, the agent speaks, you say something, it repeats it back.

Scripted use: every question has a flag, and `--yes` or `--password-stdin` turn off all prompts (clients are only touched when you pass `--clients`).

```bash
echo "$AGENT_PASSWORD" | callmaibe setup --agent-uri my-agent --callee-uri me@sip.linphone.org \
    --backend pjsua2 --clients claude-code,codex --password-stdin
callmaibe setup --yes --agent-uri sip:a@sip.linphone.org --callee-uri sip:me@sip.linphone.org --runner wsl --wsl-distro Debian
callmaibe setup --dry-run --clients claude-code,cursor     # print every action, write nothing
```

Flags: `--agent-uri --callee-uri --transport --backend --runner --wsl-distro --clients --password-stdin --dry-run --yes`. `--clients` takes `claude-code,codex,claude-desktop,cursor,gemini,other` or `none`.

## Usage

Calling is off until you switch it on, one chat session at a time. In Claude Code:

| Command | What it does |
|---|---|
| `/call on` | This session's agent may phone you when it judges it useful. |
| `/call off` | This session stops calling. |
| `/call now` | One call right now about the current work, valid for 10 minutes, even if the session is off. |
| `/call status` | Shows whether calling is allowed here and why not. |

Global controls, from any terminal (they work with no agent and no daemon running):

```bash
callmaibe off          # kill switch: block every session
callmaibe on           # lift the kill switch (sessions still need /call on)
callmaibe snooze 2h    # block calls for a while (30m, 2h, 90s)
callmaibe unsnooze
callmaibe status       # policy, session key, how the daemon runs, daemon health
```

**Quiet hours** block calls even in a session where calling is on, including `/call now`:

```toml
[policy]
quiet_hours = [
  { days = "mon-fri", from = "09:00", to = "17:00" },   # work hours
  { days = "mon-sun", from = "23:00", to = "07:00" },   # overnight, ranges may wrap midnight
]
```

**Decline to snooze:** if you decline the call on your phone, calling is snoozed for `decline_snooze_min` (default 30) minutes. That is your remote off switch.

**Local mode** (no SIP, no accounts) uses your own mic and speaker:

```bash
callmaibe setup --backend local --yes
callmaibe test-call
```

More detail: [docs/usage.md](docs/usage.md).

## Session behaviour

Claude Code gives hooks a session id but gives MCP servers none. So callmaibe identifies a session by the **Claude Code process**: both the `/call` hook and the MCP server look for the nearest ancestor process named `claude` (falling back to `node`) and use `claude-<pid>` as the key. In a plain terminal the key is `local`. What follows from that:

- After `claude --resume` or `claude --continue` there is a new process, so calling is off again. Type `/call on` again.
- Two Claude Code windows are two sessions, each with its own switch.
- Process ids can be reused after a reboot; "on" entries older than 7 days are pruned, and the global switch and quiet hours still apply.
- Set `CALLMAIBE_SESSION=name` to override the key (for a client that is not Claude Code, or to share one switch across processes). `callmaibe status` prints the current session key.

## Configuration reference

The file is `config.toml` in the callmaibe home directory (see [Where things live](#where-things-live)). Every key is optional; this is the full set with defaults. Unknown keys are an error.

```toml
[account]                      # the account the agent calls FROM
uri = ""                       # "sip:agent@sip.linphone.org"
username = ""                  # auth user; empty = the user part of uri
password_env = "CALLMAIBE_SIP_PASSWORD"   # optional env var; overrides the keyring entry
transport = "tls"              # tls | tcp | udp
stun = ["stun.linphone.org"]
turn_server = ""               # "host:port"; empty = no TURN
turn_username = ""
turn_password_env = "CALLMAIBE_TURN_PASSWORD"

[callee]
uri = ""                       # YOU: "sip:me@sip.linphone.org". The only address the agent can ever call.

[media]
backend = "pjsua2"             # pjsua2 (real SIP) | local (mic/speaker) | fake (tests)
encryption = "srtp"            # srtp | none ("none" only for debugging; Linphone rejects it with 488)
ice = true
codecs = ["G722/16000", "PCMU/8000", "PCMA/8000"]

[tts]
engine = "kokoro"
voice = "af_heart"
speed = 1.0
variant = "int8"               # kokoro model: int8 (88 MB) | fp32 (310 MB)

[stt]
engine = "faster-whisper"
model = "small"                # tiny | base | small | medium ...
language = "en"
device = "auto"                # auto | cpu | cuda
compute_type = "int8"

[conversation]
ring_timeout_s = 45            # give up if nobody answers
listen_timeout_s = 20          # no speech at all in a turn -> status no_reply
end_silence_ms = 1200          # silence that ends your turn
max_turn_s = 60
idle_hangup_s = 120            # agent silent this long between turns -> hang up
max_call_min = 20
thinking_cue = true            # soft sound while the agent thinks
keep_transcripts = true

[policy]
enabled = true                 # global kill switch default; sessions still need /call on
decline_snooze_min = 30
once_permit_min = 10           # how long /call now stays valid
quiet_hours = [
  { days = "mon-fri", from = "09:00", to = "17:00" },
]

[daemon]
host = "127.0.0.1"
port = 47823
runner = "auto"                # auto | native | wsl (wsl only matters on Windows)
wsl_distro = ""                # empty = WSL default distro
wsl_python = "~/.venvs/callmaibe/bin/python"
```

## Troubleshooting

**The call rings but there is no audio (NAT).** Signalling worked, media did not. The free Linphone accounts ship with STUN only. If your PC is behind a home router and the phone is on mobile data (often symmetric carrier NAT), STUN alone can fail. Options, cheapest first:
- Put the phone on the same Wi-Fi to confirm that NAT is the cause.
- Use a TURN relay on the PC side (one relayed side is enough): set `turn_server`, `turn_username` and the `CALLMAIBE_TURN_PASSWORD` env var in `[account]`. Cloudflare Realtime TURN has a free tier; coturn on a VPS also works.
- Use Tailscale on both devices, or any VPN that gives them a direct path.
- On Windows, enable WSL mirrored networking (`networkingMode=mirrored` in `.wslconfig`, then `wsl --shutdown`); the default NAT inside WSL is an extra hop.

**488 Not Acceptable Here.** Encryption mismatch. The Linphone apps require SRTP on sip.linphone.org accounts. Keep `media.encryption = "srtp"` and `account.transport = "tls"`. `"none"` is for other providers.

**The daemon does not start.** Run it in the foreground to see the error: `callmaibe daemon`. Check `callmaibe status` (it shows how the daemon runs: native or WSL). On Windows with `runner = "wsl"`, test the WSL side directly: `wsl.exe --exec sh -c '$HOME/.venvs/callmaibe/bin/python -m callmaibe status'`. Common causes: the WSL venv is missing (re-run `scripts\install.ps1`), `wsl_distro` names a distro that does not exist, or port 47823 is taken (change `daemon.port`). Also look at the logs directory below.

### Where things live

All under one home directory: `CALLMAIBE_HOME` if set, otherwise `%LOCALAPPDATA%\callmaibe` (Windows), `~/.local/share/callmaibe` (Linux), `~/Library/Application Support/callmaibe` (macOS).

| What | Path |
|---|---|
| Config | `<home>/config.toml` |
| Policy state (switches, snooze) | `<home>/policy.json` |
| Logs | `<home>/logs/` |
| Transcripts | `<home>/transcripts/` |
| Speech models | `<home>/models/` |
| Daemon token and lock | `<home>/daemon.token`, `<home>/daemon.lock` |

On Windows with the WSL daemon, both sides use the same Windows folder (the daemon sees it as `/mnt/c/...`).

## Privacy and security

- No MCP tool accepts a phone number or SIP address. The callee comes from `config.toml` only, so a prompt injection cannot make the agent call someone else.
- Only you can enable calling. There is no tool that switches it on; `/call` is handled by a hook or a shell step that runs on your input, and the daemon enforces the result.
- Speech is local by default (Kokoro and faster-whisper). Nothing is sent to a speech cloud unless you configure one.
- Transcripts are stored locally in `<home>/transcripts/`; turn them off with `keep_transcripts = false`. Audio is not stored.
- The daemon listens on 127.0.0.1 only and needs a random token from a user-only file. Inbound calls are rejected.
- The SIP password lives in the OS keyring (`callmaibe password set`), or in an environment variable on headless machines. It is never written to the config.
- Whoever answers your phone can steer the agent. A spoken yes can approve actions, including destructive ones. The skill makes the agent read the exact action back and hear a clear yes first; treat the phone like a keyboard.

## Technologies used

Licenses are those the upstream projects state; check them before redistributing.

| Component | Used for | License |
|---|---|---|
| [PJSIP / pjsua2](https://www.pjsip.org) | SIP stack, calls, SRTP, ICE (via the community `pjsua2-python` wheel) | GPL-2.0-or-later (or commercial) |
| [Linphone](https://linphone.org) app | The phone side (push, CallKit) | GPL-3.0 |
| sip.linphone.org (Flexisip) | Free SIP accounts, proxy and push gateway | Flexisip: AGPL-3.0; service run by Belledonne |
| [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) via [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) | Text-to-speech | model Apache-2.0, kokoro-onnx MIT |
| [onnxruntime](https://onnxruntime.ai) | Runs Kokoro on CPU | MIT |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and CTranslate2 | Speech-to-text | MIT |
| [webrtcvad](https://github.com/wiseman/py-webrtcvad) (`webrtcvad-wheels`) | Voice activity detection | MIT (WebRTC code: BSD) |
| [soxr](https://github.com/dofuuz/python-soxr) | Resampling between 8, 16 and 24 kHz | LGPL-2.1-or-later |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) / PortAudio | Local mic/speaker mode | MIT |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | MCP server for the agent | MIT |
| [uv](https://docs.astral.sh/uv/) | Python and tool installs | MIT or Apache-2.0 |
| [WSL2](https://learn.microsoft.com/windows/wsl/) | Linux runtime for the SIP daemon on Windows | part of Windows (Linux kernel: GPL-2.0) |

## Project status and roadmap

Where this stands:

- Built and unit-tested with mocks: policy (switches, snooze, quiet hours), the daemon API, the conversation engine against a fake telephony backend, the local mic/speaker backend, the speech pipeline, the CLI, the `/call` hook, and the setup wizard.
- Not yet verified end to end: real SIP calls from the pjsua2 backend to the Linphone app. That includes push wake-up with the app closed, SRTP against the app's mandatory-encryption default, and NAT on mobile data (whether sip.linphone.org relays media for free accounts is not confirmed).
- Not tested: the macOS SIP wheel, and the installers have only been dry-run (`install.ps1 -DryRun`, `bash -n` on `install.sh`).
- Known limits: the Linux pjsua2 wheel bundles OpenSSL 1.0.2 (end of life) and has no Opus; SIP needs Python 3.12.

Roadmap: finish the real-call verification, barge-in and sentence-streamed TTS polish, CI on three OSes, PyPI release, our own pjsua2 wheels (OpenSSL 3, Opus), inbound calls, PSTN through a SIP trunk, SIP MESSAGE commands, other agents. See [docs/PLAN.md](docs/PLAN.md).

## Contributing

```bash
uv sync
uv run pytest                  # default run skips the markers below
uv run pytest -m slow          # downloads models, runs real speech engines
uv run pytest -m sip           # needs pjsua2 (Linux) and network
uv run ruff check
```

Telephony is mocked in the default tests. Do not put credentials in the repo. Architecture and decisions: [docs/PLAN.md](docs/PLAN.md); daemon API: [docs/API.md](docs/API.md).

## License

GPL-3.0-or-later, chosen to fit pjsua2 (GPLv2+) and liblinphone (AGPLv3). See `LICENSE`.
