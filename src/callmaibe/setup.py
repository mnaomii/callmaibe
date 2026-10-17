"""`callmaibe setup`: one interactive installer step.

Asks for the SIP addresses and password, picks the backend, installs callmaibe into
the AI clients found on this machine, writes config.toml and offers a test call.
Everything also works through flags (`--yes`, `--clients`, `--password-stdin`), and
`--dry-run` prints every action without writing anything.

Every file it changes is backed up first (`<file>.bak-YYYYmmdd-HHMMSS`), only the
callmaibe entry is touched, and a second run changes nothing. The password goes to the
OS keyring only (through `credentials`) and is never printed or logged.
"""

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config, credentials, paths, runtime

CHOICES = {
    "transport": ("tls", "tcp", "udp"),
    "backend": ("pjsua2", "local", "fake"),
    "runner": ("auto", "native", "wsl"),
}
DEFAULT_HOST = "sip.linphone.org"
DEFAULT_MARKETPLACE = "callmaibe-marketplace"
GITHUB_SOURCE = "mnaomii/callmaibe"
TOOL_TIMEOUT_MS = "600000"
MARK_START, MARK_END = "<!-- callmaibe:start -->", "<!-- callmaibe:end -->"

INSTRUCTIONS = """\
You can phone the user with call_start, call_say and call_end, but only if call_status says
calling is available; the user switches that on. Call when a long task finishes, when you are
blocked on a decision, or when you need approval for a risky step. Speak in short plain sentences,
one question at a time, no code or URLs. Read back any destructive action and get a clear yes
before doing it. If calling is unavailable, write the summary in the terminal instead."""

# id -> label; "other" only prints a snippet.
CLIENTS = {
    "claude-code": "Claude Code (plugin)",
    "codex": "Codex CLI",
    "claude-desktop": "Claude Desktop",
    "cursor": "Cursor",
    "gemini": "Gemini CLI",
    "other": "Other (print a JSON snippet)",
}
SESSIONS = {"codex": "codex", "claude-desktop": "claude-desktop", "cursor": "cursor",
            "gemini": "gemini", "other": "NAME"}

Runner = Callable[[list[str]], tuple[int, str]]


# ---------------------------------------------------------------- paths (tests point these at tmp_path)

def user_home() -> Path:
    return Path.home()


def claude_dir() -> Path:
    return user_home() / ".claude"


def codex_dir() -> Path:
    return user_home() / ".codex"


def cursor_dir() -> Path:
    return user_home() / ".cursor"


def gemini_dir() -> Path:
    return user_home() / ".gemini"


def desktop_config() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or user_home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = user_home() / "Library" / "Application Support"
    else:
        base = user_home() / ".config"
    return base / "Claude" / "claude_desktop_config.json"


# ---------------------------------------------------------------- toml writer for config.toml

def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_val(x)}" for k, x in v.items()) + " }"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def dumps(data: dict) -> str:
    """Tiny TOML writer: top-level tables of scalars/lists (lists of tables go inline)."""
    out = []
    for section, body in data.items():
        out.append(f"[{section}]")
        out += [f"{k} = {_val(v)}" for k, v in body.items()]
        out.append("")
    return "\n".join(out)


def apply(data: dict, *, agent_uri, callee_uri, transport, backend, runner, wsl_distro) -> dict:
    """Update only the keys the wizard manages; leave everything else as it was."""
    for section, key, value in [
        ("account", "uri", agent_uri), ("callee", "uri", callee_uri),
        ("account", "transport", transport), ("media", "backend", backend),
        ("daemon", "runner", runner), ("daemon", "wsl_distro", wsl_distro),
    ]:
        if value is not None:
            data.setdefault(section, {})[key] = value
    return data


# ---------------------------------------------------------------- small helpers

_URI_RE = re.compile(r"^[A-Za-z0-9._+\-]+@[A-Za-z0-9.\-]+(:\d+)?$")


def normalize_uri(text: str) -> str:
    """`my-agent`, `my-agent@host` or `sip:my-agent@host` -> `sip:user@host`. Raises ValueError."""
    t = text.strip()
    if t.lower().startswith("sip:"):
        t = t[4:]
    if "@" not in t and t:
        t += "@" + DEFAULT_HOST
    if not _URI_RE.match(t):
        raise ValueError(f"not a SIP address: {text!r} (use name, name@host or sip:name@host)")
    user, host = t.split("@", 1)
    return f"sip:{user}@{host.lower()}"


def _run_command(cmd: list[str]) -> tuple[int, str]:
    """Default command runner: (exit code, combined output). Never raises."""
    exe = shutil.which(cmd[0]) or cmd[0]
    try:
        r = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=300,
                           check=False, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def mcp_command() -> tuple[str, list[str]]:
    """(command, args) that start the MCP server, with an absolute path where possible."""
    exe = shutil.which("callmaibe")
    if not exe:
        for name in ("callmaibe.exe", "callmaibe"):
            cand = Path(sys.executable).parent / name
            if cand.is_file():
                exe = str(cand)
                break
    if exe:
        return os.path.abspath(exe), ["mcp"]
    return sys.executable, ["-m", "callmaibe", "mcp"]


def find_marketplace() -> tuple[str, str]:
    """(marketplace source, marketplace name): this checkout if there is one, else GitHub."""
    for parent in Path(__file__).resolve().parents:
        mp = parent / ".claude-plugin" / "marketplace.json"
        if mp.is_file():
            try:
                name = json.loads(mp.read_text(encoding="utf-8")).get("name") or DEFAULT_MARKETPLACE
            except ValueError:
                name = DEFAULT_MARKETPLACE
            return str(parent), name
    return GITHUB_SOURCE, DEFAULT_MARKETPLACE


@dataclass
class Ctx:
    dry: bool
    interactive: bool
    run: Runner

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def do(self, cmd: list[str]) -> tuple[int, str]:
        """Run a command that changes things (printed; skipped on --dry-run)."""
        print(f"  $ {' '.join(cmd)}")
        if self.dry:
            print("    (dry run: not executed)")
            return 0, ""
        return self.run(cmd)

    def ask(self, prompt: str, default: str = "", show_default: bool = True) -> str:
        if not self.interactive:
            return default
        shown = (f" [{default}]" if show_default else " [Enter keeps the current one]") if default else ""
        try:
            return input(f"{prompt}{shown}: ").strip() or default
        except EOFError:
            return default

    def yes(self, prompt: str, default: bool) -> bool:
        if not self.interactive:
            return default
        a = self.ask(f"{prompt} [{'Y/n' if default else 'y/N'}]").lower()
        return default if not a else a.startswith("y")


def write_text(ctx: Ctx, path: Path, text: str, what: str) -> None:
    """Write `path`, backing up the old file first. `what` says what changed."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        print(f"  {path}: unchanged")
        return
    print(f"  {path}: {what}")
    if ctx.dry:
        print("    (dry run: not written)")
        return
    if path.exists():
        bak = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, bak)
        print(f"    backup: {bak}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def merge_json(ctx: Ctx, path: Path, keys: list[str], value, label: str = "") -> bool:
    """Set a nested key in a JSON file, keeping everything else. False if it could not."""
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            print(f"  ! {path} is not a JSON object; left untouched")
            return False
    node = data
    for k in keys[:-1]:
        node = node.setdefault(k, {})
        if not isinstance(node, dict):
            print(f"  ! {path}: '{k}' is not an object; left untouched")
            return False
    name = ".".join(keys)
    if node.get(keys[-1]) == value:
        print(f"  {path}: {name} already up to date")
        return True
    verb = "replace" if keys[-1] in node else "add"
    node[keys[-1]] = value
    write_text(ctx, path, json.dumps(data, indent=2, ensure_ascii=False) + "\n",
               f"{verb} {name} = {label or json.dumps(value)}")
    return True


def server_entry(exe: str, margs: list[str], session: str, **extra) -> dict:
    return {"command": exe, "args": margs, "env": {"CALLMAIBE_SESSION": session}, **extra}


def snippet(exe: str, margs: list[str], session: str) -> str:
    return json.dumps({"mcpServers": {"callmaibe": server_entry(exe, margs, session)}}, indent=2)


# ---------------------------------------------------------------- clients

def detect(ctx: Ctx, client: str) -> bool:
    match client:
        case "claude-code":
            return bool(ctx.which("claude"))
        case "codex":
            return bool(ctx.which("codex")) or codex_dir().exists()
        case "claude-desktop":
            return desktop_config().parent.exists()
        case "cursor":
            return cursor_dir().exists()
        case "gemini":
            return bool(ctx.which("gemini")) or gemini_dir().exists()
    return False


def install_claude_code(ctx: Ctx, exe: str, margs: list[str]) -> None:
    source, name = find_marketplace()
    steps = [["claude", "plugin", "marketplace", "add", source, "--scope", "user"],
             ["claude", "plugin", "install", f"callmaibe@{name}", "--scope", "user"]]
    ok = True
    for cmd in steps:
        rc, out = ctx.do(cmd)
        if rc and not re.search(r"already", out, re.IGNORECASE):
            print(f"    failed (exit {rc}): {out[-300:]}")
            ok = False
            break
    if ok:
        print("  Claude Code plugin " + ("would be installed" if ctx.dry else "installed")
              + ": /call, the hook and the phone-call skill.")
        if not ctx.which("callmaibe"):
            print("  note: the plugin starts `callmaibe` from PATH; open a new terminal so PATH is "
                  "refreshed.")
    else:
        print("  Plugin install failed; falling back to a plain MCP server (no /call without the plugin).")
        rc, out = ctx.do(["claude", "mcp", "add", "--scope", "user", "callmaibe", "--",
                          exe, *margs])
        if rc and not re.search(r"already exists", out, re.IGNORECASE):
            print(f"    failed (exit {rc}): {out[-300:]}")
        else:
            print("  MCP server added. For /call and the skill, install the plugin later:\n"
                  f"    claude plugin marketplace add {source}\n"
                  f"    claude plugin install callmaibe@{name}")
    if ctx.yes("Raise Claude Code's MCP tool timeout to 10 minutes "
               "(a call blocks while ringing and listening)?", True):
        merge_json(ctx, claude_dir() / "settings.json", ["env", "MCP_TOOL_TIMEOUT"], TOOL_TIMEOUT_MS)


def _replace_block(text: str, block: str) -> str:
    pat = re.compile(re.escape(MARK_START) + r".*?" + re.escape(MARK_END), re.DOTALL)
    if pat.search(text):
        return pat.sub(lambda _m: block, text, count=1)
    return text + ("" if not text else ("\n" if text.endswith("\n") else "\n\n")) + block + "\n"


def install_codex(ctx: Ctx, exe: str, margs: list[str]) -> None:
    cfg = codex_dir() / "config.toml"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    if re.search(r"^\s*\[mcp_servers\.callmaibe[\].]", text, re.MULTILINE):
        print(f"  {cfg}: [mcp_servers.callmaibe] already present")
    else:
        done = False
        if ctx.which("codex"):
            rc, out = ctx.do(["codex", "mcp", "add", "callmaibe", "--env", "CALLMAIBE_SESSION=codex",
                              "--", exe, *margs])
            done = rc == 0
            if done:
                print("  note: add `tool_timeout_sec = 600` under [mcp_servers.callmaibe] in "
                      f"{cfg} (a call blocks while ringing and listening).")
            else:
                print(f"    failed (exit {rc}): {out[-300:]}; writing config.toml directly")
        if not done:
            block = ("[mcp_servers.callmaibe]\n"
                     f"command = {_val(exe)}\nargs = {_val(margs)}\n"
                     'env = { CALLMAIBE_SESSION = "codex" }\ntool_timeout_sec = 600\n')
            new = text + ("" if not text else ("\n" if text.endswith("\n") else "\n\n")) + block
            write_text(ctx, cfg, new, "append [mcp_servers.callmaibe]")
    agents = codex_dir() / "AGENTS.md"
    old = agents.read_text(encoding="utf-8") if agents.exists() else ""
    new = _replace_block(old, f"{MARK_START}\n{INSTRUCTIONS}\n{MARK_END}")
    if new == old:
        print(f"  {agents}: instructions already up to date")
    else:
        write_text(ctx, agents, new,
                   "replace callmaibe instructions" if MARK_START in old else "add callmaibe instructions")


def install_json_client(ctx: Ctx, client: str, exe: str, margs: list[str]) -> None:
    session = SESSIONS[client]
    if client == "claude-desktop":
        path, entry = desktop_config(), server_entry(exe, margs, session)
    elif client == "cursor":
        path, entry = cursor_dir() / "mcp.json", server_entry(exe, margs, session)
    else:
        path, entry = gemini_dir() / "settings.json", server_entry(exe, margs, session, timeout=600000)
    merge_json(ctx, path, ["mcpServers", "callmaibe"], entry)


def install_other(exe: str, margs: list[str]) -> None:
    print("  Add this to the MCP config of your client:\n")
    print(snippet(exe, margs, SESSIONS["other"]))
    print("\n  and put this in its instructions file (AGENTS.md or similar):\n")
    print(INSTRUCTIONS)


def install_client(ctx: Ctx, client: str, exe: str, margs: list[str]) -> None:
    print(f"\n{CLIENTS[client]}")
    if client == "claude-code":
        install_claude_code(ctx, exe, margs)
    elif client == "codex":
        install_codex(ctx, exe, margs)
    elif client == "other":
        install_other(exe, margs)
    else:
        install_json_client(ctx, client, exe, margs)
    if client not in ("claude-code", "other"):
        print(f"  Turn calling on (no /call here): callmaibe session on --session {SESSIONS[client]}")


def pick_clients(ctx: Ctx, args: argparse.Namespace) -> list[str]:
    if args.clients is not None:
        return args.clients
    if not ctx.interactive:
        return []
    ids = list(CLIENTS)
    pre = {c for c in ids if detect(ctx, c)}
    print("\nInstall callmaibe into which clients? (found ones are marked)")
    for i, c in enumerate(ids, 1):
        print(f"  {i}) [{'x' if c in pre else ' '}] {CLIENTS[c]}")
    while True:
        raw = ctx.ask("Numbers separated by commas, Enter keeps the marked ones, 0 for none")
        if not raw:
            return [c for c in ids if c in pre]
        try:
            nums = [int(x) for x in raw.replace(" ", "").split(",") if x]
        except ValueError:
            nums = [-1]
        if nums == [0]:
            return []
        if nums and all(1 <= n <= len(ids) for n in nums):
            return [ids[n - 1] for n in dict.fromkeys(nums)]
        print(f"  enter numbers 1-{len(ids)} separated by commas")


def parse_clients(text: str) -> list[str]:
    out = [c.strip() for c in text.split(",") if c.strip() and c.strip() != "none"]
    bad = [c for c in out if c not in CLIENTS]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown client {bad[0]!r}; choose from {', '.join(CLIENTS)}")
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- password

def step_password(ctx: Ctx, args: argparse.Namespace, agent_uri: str, env_name: str) -> bool:
    """Returns True when a password will be available to the daemon."""
    env_set = bool(os.environ.get(env_name))
    backend = credentials.backend_name()
    if not backend:
        print(f"No OS keyring here, so nothing is stored. Set the environment variable {env_name} "
              "to the agent account password before a call (it is read at call time).")
        return env_set
    key = credentials.sip_key(agent_uri)
    pw = None
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\r\n")
        if not pw:
            print("  ! empty password on stdin; nothing stored")
            return env_set or bool(credentials.get(key))
    existing = credentials.get(key)
    if pw is None:
        if not ctx.interactive:
            print("password: kept the stored one" if existing
                  else "password: not set yet; run `callmaibe password set`")
            return bool(existing) or env_set
        if existing and not ctx.yes(f"A password for {agent_uri} is already in the keyring. Replace it?", False):
            print("  keeping the stored password")
            return True
        for _ in range(3):
            first = getpass.getpass(f"Password for {agent_uri} (hidden): ")
            if first and first == getpass.getpass("Again: "):
                pw = first
                break
            print("  empty or not matching, try again")
        if pw is None:
            print("  nothing stored; run `callmaibe password set` later")
            return bool(existing) or env_set
    if ctx.dry:
        print(f"  (dry run) would store the password in the keyring ({backend})")
    else:
        credentials.set(key, pw)
        print(f"  password stored in the keyring ({backend})")
    if env_set:
        print(f"  note: env var {env_name} is also set and takes precedence")
    return True


# ---------------------------------------------------------------- wizard

def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--agent-uri", help="SIP address the agent calls from: my-agent, my-agent@host or sip:my-agent@host")
    sp.add_argument("--callee-uri", help="SIP address of your phone account (same forms)")
    sp.add_argument("--transport", choices=CHOICES["transport"])
    sp.add_argument("--backend", choices=CHOICES["backend"])
    sp.add_argument("--runner", choices=CHOICES["runner"])
    sp.add_argument("--wsl-distro", help="WSL distro that runs the daemon (default distro if empty)")
    sp.add_argument("--clients", type=parse_clients, metavar="LIST",
                    help=f"comma list to install into: {','.join(CLIENTS)} (or none)")
    sp.add_argument("--password-stdin", action="store_true",
                    help="read the agent password as one line from stdin")
    sp.add_argument("--dry-run", action="store_true", help="print every action, write nothing")
    sp.add_argument("--yes", action="store_true", help="no prompts; use flags and existing values")


def _ask_uri(ctx: Ctx, prompt: str, default: str, avoid: str = "") -> str:
    while True:
        raw = ctx.ask(prompt, default, show_default=False)   # don't echo real addresses on screen
        try:
            uri = normalize_uri(raw)
        except ValueError as e:
            print(f"  {e}")
            continue
        if avoid and uri.lower() == avoid.lower():
            print("  that must differ from the agent's address")
            continue
        return uri


def run(args: argparse.Namespace, path: Path | None = None, runner: Runner | None = None) -> int:
    ctx = Ctx(dry=args.dry_run, run=runner or _run_command,
              interactive=not args.yes and not args.password_stdin and sys.stdin.isatty())
    path = path or paths.config_file()
    data: dict = {}
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)

    def cur(s: str, k: str, d: str = "") -> str:
        return data.get(s, {}).get(k, d)

    agent_uri, callee_uri = args.agent_uri, args.callee_uri
    try:
        agent_uri = normalize_uri(agent_uri) if agent_uri else None
        callee_uri = normalize_uri(callee_uri) if callee_uri else None
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if ctx.dry:
        print("DRY RUN: nothing will be written or installed.")
    print(f"callmaibe setup (config: {path})\n" + ("Press Enter to keep the value in brackets.\n"
                                                    if ctx.interactive else ""))
    sip = args.backend != "local"
    if sip:
        print("1. Agent SIP address")
        agent_uri = agent_uri or (_ask_uri(ctx, "  Agent SIP address, e.g. my-agent or sip:my-agent@sip.linphone.org",
                                           cur("account", "uri")) if ctx.interactive else None)
        print("2. Your SIP address (the phone that rings)")
        callee_uri = callee_uri or (_ask_uri(ctx, "  Your SIP address, e.g. me or sip:me@sip.linphone.org",
                                             cur("callee", "uri"),
                                             avoid=agent_uri or cur("account", "uri"))
                                    if ctx.interactive else None)
        if (agent_uri or cur("account", "uri")).lower() == (callee_uri or "-").lower():
            print("error: your address must differ from the agent's", file=sys.stderr)
            return 2
    vals = {"agent_uri": agent_uri, "callee_uri": callee_uri, "transport": args.transport,
            "backend": args.backend, "runner": args.runner, "wsl_distro": args.wsl_distro}

    uri = agent_uri or cur("account", "uri")
    cfg = config._build(config.Config, apply(json.loads(json.dumps(data)), **vals), "config")
    pw_ok = False
    if sip:
        print("3. Agent password")
        if uri:
            pw_ok = step_password(ctx, args, uri, cfg.account.password_env)
        else:
            print("  no agent address yet; set one, then run `callmaibe password set`")

    print("4. Backend")
    if not args.backend and ctx.interactive:
        vals["backend"] = ctx.ask("  Backend: pjsua2 = real phone calls, local = laptop mic and speaker",
                                  cur("media", "backend", "pjsua2"))
        while vals["backend"] not in ("pjsua2", "local", "fake"):
            vals["backend"] = ctx.ask("  pick pjsua2 or local", "pjsua2")
    backend = vals["backend"] or cur("media", "backend", "pjsua2")
    if sys.platform == "win32" and backend == "pjsua2" and not args.runner:
        vals["runner"] = "wsl"
    cfg = config._build(config.Config, apply(json.loads(json.dumps(data)), **vals), "config")
    if runtime.uses_wsl(cfg):
        py = cfg.daemon.wsl_python
        script = 'p="$1"; case "$p" in "~/"*) p="$HOME/${p#"~/"}";; esac; test -x "$p"'
        distro = ["-d", cfg.daemon.wsl_distro] if cfg.daemon.wsl_distro else []
        rc, _ = ctx.run(["wsl.exe", *distro, "--exec", "sh", "-c", script, "sh", py])
        if rc:
            print(f"  ! {py} not found in WSL ({cfg.daemon.wsl_distro or 'default distro'}). "
                  "Run scripts/install.ps1 to set up the WSL side; continuing anyway.")
        else:
            print(f"  WSL side found ({py})")
    print(f"  backend: {backend}; daemon runs: {runtime.describe(cfg)}")

    print("5. Clients")
    chosen = pick_clients(ctx, args)
    exe, margs = mcp_command()
    for c in chosen:
        install_client(ctx, c, exe, margs)
    if not chosen:
        print("  none selected (callmaibe setup --clients claude-code,codex,... adds them later)")

    print("\n6. Config")
    data = apply(data, **vals)
    data = {k: v for k, v in data.items() if v}
    write_text(ctx, path, dumps(data), "set " + ", ".join(
        f"{s}.{k}" for s, k, v in [("account", "uri", vals["agent_uri"]), ("callee", "uri", vals["callee_uri"]),
                                    ("account", "transport", vals["transport"]),
                                    ("media", "backend", vals["backend"]), ("daemon", "runner", vals["runner"]),
                                    ("daemon", "wsl_distro", vals["wsl_distro"])] if v is not None)
               or "nothing new (rewritten as is)")

    print("\n7. Summary")
    print(f"  agent {cfg.account.uri or '-'} -> you {cfg.callee.uri or '-'}; backend {backend}; "
          f"daemon {runtime.describe(cfg)}")
    print(f"  clients: {', '.join(chosen) or 'none'}")
    print(f"  password: {'available' if pw_ok else 'not set' if sip else 'not needed (local backend)'}")
    if (ctx.interactive and not ctx.dry and backend == "pjsua2" and pw_ok
            and ctx.yes("Place a test call now? Your phone will ring", False)):
        from .cli import main
        return main(["test-call"])
    print("\nNext: callmaibe test-call" if backend != "pjsua2" or pw_ok
          else "\nNext: callmaibe password set, then callmaibe test-call")
    return 0
