# callmaibe usage guide

Short version is in the [README](../README.md). This page has the details.

## Commands

| Command | Purpose |
|---|---|
| `callmaibe setup` | Interactive installer: SIP addresses, password, backend, client integrations, `config.toml`. |
| `callmaibe test-call` | Rings you, speaks, listens, repeats what you said, hangs up. |
| `callmaibe status` | Policy state, session key, how the daemon runs, daemon health, active call. |
| `callmaibe on` / `off` | Global kill switch. `off` blocks every session. |
| `callmaibe snooze 2h` | Block calls for a while. Accepts `90s`, `30m`, `2h`; a bare number is minutes. |
| `callmaibe unsnooze` | Clear the snooze. |
| `callmaibe session on\|off\|now\|status [--session KEY]` | The per-session switch, from a terminal. Used by the `/call` fallback command. |
| `callmaibe daemon` | Run the daemon in the foreground. Normally the MCP server starts it. |
| `callmaibe mcp` | The MCP server (stdio). Started by the agent, not by you. |
| `callmaibe hook` | The Claude Code `UserPromptSubmit` hook. Reads the hook JSON on stdin. |

Examples:

```bash
callmaibe setup --yes --agent-uri sip:agent@sip.linphone.org \
                      --callee-uri sip:me@sip.linphone.org --transport tls --backend pjsua2
callmaibe setup --yes --runner wsl --wsl-distro Debian      # Windows: daemon in WSL
callmaibe snooze 90m
callmaibe status
callmaibe session on --session claude-12345                 # what /call on does for that session
```

`setup` walks through seven steps: the agent's SIP address, your SIP address, the agent password, the backend, the clients to install into, `config.toml`, and a summary with an optional test call. Addresses accept `my-agent`, `my-agent@host` or `sip:my-agent@host` (a bare name gets `@sip.linphone.org`) and are stored as `sip:user@host`; yours must differ from the agent's.

- **Password.** Asked hidden, twice, stored in the OS keyring through the same code as `callmaibe password set`. An existing entry is kept unless you say replace. Without a keyring nothing is stored and it tells you to use `CALLMAIBE_SIP_PASSWORD`. Never printed or logged. `--password-stdin` reads one line from stdin instead (for scripts); it also turns off all prompts.
- **Backend.** `pjsua2` or `local`. On Windows with `pjsua2` the runner becomes `wsl` and it checks that `daemon.wsl_python` exists in the distro (`wsl.exe [-d DISTRO] --exec sh -c ...`); if not, it points to `scripts/install.ps1` and carries on.
- **Clients.** Found ones are preselected: Claude Code (`claude` on `PATH`), Codex CLI (`codex` on `PATH` or `~/.codex`), Claude Desktop (its config folder), Cursor (`~/.cursor`), Gemini CLI (`gemini` on `PATH` or `~/.gemini`), plus Other, which only prints a snippet. Choose by number, or with `--clients claude-code,codex,claude-desktop,cursor,gemini,other` (`none` for nothing). Without a terminal, `--yes` or `--password-stdin`, clients are only touched when `--clients` is given.
  - Claude Code: `claude plugin marketplace add <this checkout, else mnaomii/callmaibe> --scope user`, then `claude plugin install callmaibe@callmaibe-marketplace --scope user`. If either fails: `claude mcp add --scope user callmaibe -- <callmaibe> mcp` (then `/call` needs the plugin). It also offers (default yes) to set `env.MCP_TOOL_TIMEOUT = "600000"` in `~/.claude/settings.json`.
  - Codex CLI: `codex mcp add callmaibe --env CALLMAIBE_SESSION=codex -- <callmaibe> mcp`, or an appended `[mcp_servers.callmaibe]` block in `~/.codex/config.toml` (with `tool_timeout_sec = 600`) when `codex` is not on `PATH`; skipped if the section exists. The instructions go into `~/.codex/AGENTS.md` between `<!-- callmaibe:start -->` and `<!-- callmaibe:end -->` and are replaced, not duplicated, on re-runs.
  - Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`, `~/Library/Application Support/Claude/...` on macOS, `~/.config/Claude/...` on Linux), Cursor (`~/.cursor/mcp.json`) and Gemini CLI (`~/.gemini/settings.json`, with `"timeout": 600000`): `mcpServers.callmaibe` with `CALLMAIBE_SESSION` set to `claude-desktop`, `cursor` or `gemini`.
  - The command in every entry is the absolute path of `callmaibe` (from `PATH`, else next to the Python that runs the wizard, else `python -m callmaibe`).
  - Before changing a file it copies it to `<file>.bak-YYYYmmdd-HHMMSS`. Only the callmaibe entry changes; JSON files are rewritten with 2-space indentation. A file that is not valid JSON is left alone. Re-running changes nothing and makes no new backup.
  - Clients other than Claude Code have no `/call`, so it prints `callmaibe session on --session <name>` for each.
- **Config.** `setup` only changes the keys it manages (`account.uri`, `account.transport`, `callee.uri`, `media.backend`, `daemon.runner`, `daemon.wsl_distro`) and keeps everything else in an existing `config.toml`. Run it again any time to change values.
- **Dry run.** `--dry-run` prints every command and file change and writes nothing (read-only checks, such as the WSL probe, still run).
- **Test call.** Offered only for `pjsua2` with a password available: `Place a test call now? Your phone will ring [y/N]`, default no.

The policy commands (`on`, `off`, `snooze`, `unsnooze`, `session`) edit `policy.json` directly. They work with the daemon down, and the daemon re-reads the file on every check.

## The `/call` command in Claude Code

`/call on | off | now | status` must come from you and never from the model, so it is handled by code that only runs on your input. The plugin ships two mechanisms:

1. **`UserPromptSubmit` hook (primary).** `plugin/hooks/hooks.json` runs `callmaibe hook` on every prompt. The hook reads the prompt from stdin and ignores anything that is not `/call ...` (it also accepts the namespaced `/callmaibe:call`).
   - `on`, `off`, `status`: the hook applies the change, then blocks the prompt with the result as the message, so no model turn happens and the model never sees the command.
   - `now`: the hook grants a one-time permit and lets the prompt through with extra context telling the agent to phone you about the current work.
   - Anything else gets a usage message.
2. **`commands/call.md` (fallback).** If the hook is not active, the slash command itself runs `callmaibe session $ARGUMENTS` in its pre-run shell step (`!` backtick syntax, limited to `Bash(callmaibe session *)`) and asks the model to repeat the result. The command has `disable-model-invocation: true`, and the model is told never to run `callmaibe session` itself.

Either way, the session being switched is the Claude Code process that the hook or command runs under, see below.

## Session identity

Claude Code passes hooks a `session_id` but gives MCP servers none (only `CLAUDE_PROJECT_DIR`). The switch has to match between the hook (which flips it) and the MCP server (which the daemon checks it for), so callmaibe keys on something both can see: the Claude Code process.

- Both walk up their process ancestry to the nearest process named `claude` / `claude.exe` and use `claude-<pid>`. If there is none, the nearest `node` ancestor is used (npm installs of Claude Code). In a plain terminal the key is `local`.
- `CALLMAIBE_SESSION` overrides the key everywhere it is set. Use it for non-Claude clients: set the same value in the MCP server's environment and in the terminal where you run `callmaibe session on`.
- `claude --resume` and `claude --continue` start a new process, so calling is off again. Type `/call on` again.
- Two windows are two sessions with separate switches.
- PIDs can repeat after a reboot, so a stale "on" could match a new session. Entries older than 7 days are pruned, and the global switch and quiet hours still apply.
- The node fallback can match an unrelated node ancestor in odd setups. If `callmaibe status` shows a surprising key, set `CALLMAIBE_SESSION`.

`callmaibe status` prints `session key: ...` so you can see what a terminal resolves to. Note that a terminal outside Claude Code resolves to `local`, which is not your Claude session; use `callmaibe session on --session claude-<pid>` to target one.

## Policy: order of checks

Calling is decided in the daemon on every `call_start`, in this order. The first failing check wins and its reason is returned to the agent as `status: "unavailable"`:

1. **Global switch.** `callmaibe off` (or `policy.enabled = false` before you ever ran `callmaibe on`) blocks everything.
2. **Snooze.** `callmaibe snooze ...`, or an automatic snooze after you declined a call (`decline_snooze_min`).
3. **Quiet hours.** Any matching `policy.quiet_hours` range. These beat `/call now` too.
4. **Session switch.** Allowed only if this session is on (`/call on`) or has an unexpired one-time permit (`/call now`, valid `once_permit_min` minutes, consumed by a successful call start).

When calls are unavailable the agent is told to write its summary in the terminal instead. A declined call counts as busy, triggers the decline snooze, and the agent must not retry.

Quiet-hours `days` accepts `"mon-fri"`, `"sat,sun"`, `"tue"`, `"fri-mon"` (wraps over the weekend). A range whose `to` is earlier than `from` runs over midnight and belongs to the day it started; equal times mean the whole day.

## Windows with WSL

The pjsua2 Windows wheels lack TLS, so real SIP calls need Linux. On Windows the SIP daemon runs inside WSL2 and the CLI, MCP server and hooks stay native. `runtime.py` decides this when the daemon is spawned, from `[daemon]` and `[media]`:

| Setting | Meaning |
|---|---|
| `daemon.runner = "auto"` | WSL when on Windows and `media.backend = "pjsua2"`, otherwise native. |
| `daemon.runner = "native"` | Always native (the `local` and `fake` backends, or if you run Windows-native pjsua2). |
| `daemon.runner = "wsl"` | Always WSL (Windows only). |
| `daemon.wsl_distro` | Distro for `wsl.exe -d`. Empty means the WSL default. |
| `daemon.wsl_python` | Python inside WSL, default `~/.venvs/callmaibe/bin/python` (`~/` becomes `$HOME/`). |

The command it runs is `wsl.exe [-d DISTRO] --exec sh -c 'CALLMAIBE_HOME="$1" exec <python> -m callmaibe daemon' sh /mnt/c/...`. Details:

- **Shared home.** The WSL daemon gets `CALLMAIBE_HOME` set to the `/mnt/<drive>/...` path of the Windows-side home (`C:\Users\me\AppData\Local\callmaibe` becomes `/mnt/c/Users/me/AppData/Local/callmaibe`). Config, policy, token, logs and transcripts are therefore the same files on both sides, and Windows-side `callmaibe on|off|snooze` take effect for the daemon immediately.
- **Passwords via `WSLENV`.** WSL has no keyring. When spawning the WSL daemon, `callmaibe` reads the SIP and TURN passwords on the Windows side (env var, else Credential Manager), puts them in the environment of that one `wsl.exe` process only, and lists their names in `WSLENV` so they cross into Linux. Nothing is written to disk or to your user environment.
- **Localhost.** The daemon listens on `127.0.0.1:47823`; with WSL2 localhost forwarding (and with mirrored networking) the Windows-side clients reach it.
- **Mirrored networking.** `networkingMode=mirrored` under `[wsl2]` in `%USERPROFILE%\.wslconfig` makes WSL share the host's network interfaces, which helps NAT traversal for SIP/RTP. Needs a recent Windows 11 and `wsl --shutdown` afterwards. `install.ps1` offers to set it and backs up the file.
- `callmaibe status` shows `daemon runs: native` or `wsl (<distro>)`.
- The `CALLMAIBE_DAEMON_CMD` environment variable replaces the whole spawn command (for testing).

To check the WSL side by hand:

```powershell
wsl.exe -d Debian --exec sh -c '$HOME/.venvs/callmaibe/bin/python -c "import callmaibe, pjsua2; print(1)"'
```

## Other MCP clients

`callmaibe setup` writes the config for Codex CLI, Claude Desktop, Cursor and Gemini CLI; `--clients other` prints the snippet and instructions for anything else. Remember that `/call` and its hook are Claude Code only. For other clients, pick a name, give it to the client as `CALLMAIBE_SESSION=name` in the MCP server environment, then run `callmaibe session on --session name` yourself. The agent still cannot switch calling on.

Calls block inside a tool call while ringing and listening (up to a minute or two), so raise the client's MCP tool timeout. The server sends progress notifications while it waits.
