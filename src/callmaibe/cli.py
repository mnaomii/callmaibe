"""callmaibe command line."""

import argparse
import json
import os
import re
import sys

from . import config, runtime
from .client import DaemonClient, DaemonError
from .policy import Policy
from .session import session_key

CALL_RE = re.compile(r"^/(?:callmaibe:)?call(?:\s+(\S+))?\s*$", re.IGNORECASE)


def parse_duration(s: str) -> float:
    """'30m', '2h', '90s' (bare number = minutes) -> minutes."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", s.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {s!r}, use e.g. 30m, 2h, 90s")
    return float(m[1]) * {"s": 1 / 60, "m": 1, "h": 60, "": 1}[m[2]]


def describe(st: dict) -> str:
    return "\n".join([
        f"calling allowed here: {'yes' if st['allowed'] else 'no'} ({st['reason']})",
        f"global switch: {'on' if st['global_enabled'] else 'off'}",
        f"this session: {'on' if st['session_enabled'] else 'off'}"
        + (" (one-time permit active)" if st.get("once_permit") else ""),
        f"snoozed until: {st['snoozed_until'] or '-'}",
        f"quiet hours now: {'yes' if st['quiet_now'] else 'no'}",
    ])


def session_action(policy: Policy, action: str, key: str) -> str:
    if action == "on":
        policy.enable_session(key, True)
        return "Calling is ON for this session. The agent may phone you when it is useful."
    if action == "off":
        policy.enable_session(key, False)
        return "Calling is OFF for this session."
    if action == "now":
        policy.grant_once(key)
        return (f"One call is permitted now (valid {policy.cfg.once_permit_min:g} min), "
                "even though calling is off for this session.")
    return describe(policy.status(key))


def cmd_status(cfg: config.Config, policy: Policy) -> int:
    key = session_key()
    print(describe(policy.status(key)))
    print(f"session key: {key}")
    print(f"daemon runs: {runtime.describe(cfg)}")
    client = DaemonClient(cfg)
    try:
        h = client.health()
        print(f"daemon: up (v{h.get('version')}, backend {h.get('backend')}, "
              f"registered {h.get('registered')})")
        print(f"active call: {client.status(key).get('active_call') or 'none'}")
    except DaemonError:
        print("daemon: not running (starts automatically on the first call)")
    return 0


def cmd_hook(policy: Policy) -> int:
    """UserPromptSubmit hook: handle the user's `/call ...` here, never via the agent."""
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except ValueError:
        return 0
    m = CALL_RE.match(str(data.get("prompt", "")).strip())
    if not m:
        return 0
    action = (m[1] or "status").lower()
    event = {"hookEventName": "UserPromptSubmit"}
    if action not in ("on", "off", "now", "status"):
        out = {"decision": "block", "reason": "Usage: /call on | off | now | status"}
    else:
        msg = session_action(policy, action, session_key())
        if action == "now":
            ctx = ("The user just permitted one phone call right now. Call them with call_start "
                   "about the current work, following the phone-call skill.")
            out = {"hookSpecificOutput": {**event, "additionalContext": ctx}}
            print(msg, file=sys.stderr)
        else:
            out = {"decision": "block", "reason": msg,
                   "hookSpecificOutput": {**event, "suppressOriginalPrompt": True}}
    print(json.dumps(out))
    return 0


def cmd_test_call(cfg: config.Config, policy: Policy) -> int:
    key = f"test-call-{os.getpid()}"
    policy.grant_once(key)
    client = DaemonClient(cfg)
    client.ensure_running()
    r = client.start(key, "This is a callmaibe test call. Say something and I will repeat it.")
    print(json.dumps(r))
    if r["status"] not in ("in_call", "no_reply"):
        return 1
    r = client.say(key, r["call_id"], f"You said: {r['user_said'] or 'nothing'}", False)
    print(json.dumps(r))
    if r["status"] not in ("ended", "ended_by_user"):
        print(json.dumps(client.end(key, r["call_id"])))
    return 0


def cmd_password(cfg: config.Config, action: str, turn: bool) -> int:
    import getpass
    import os

    from . import credentials

    acc = cfg.account
    if turn:
        key, env_name, what = credentials.turn_key(acc.turn_server, acc.turn_username), \
            acc.turn_password_env, f"TURN {acc.turn_username}@{acc.turn_server}"
    else:
        key, env_name, what = credentials.sip_key(acc.uri), acc.password_env, acc.uri
    if (turn and not acc.turn_server) or (not turn and not acc.uri):
        print("error: configure the account first (callmaibe setup)", file=sys.stderr)
        return 1
    backend = credentials.backend_name()
    if action == "status":
        in_env = bool(os.environ.get(env_name))
        in_ring = bool(credentials.get(key))
        print(f"{what}: keyring ({backend or 'none available'}): {'set' if in_ring else 'not set'}; "
              f"env {env_name}: {'set (takes precedence)' if in_env else 'not set'}")
        return 0 if in_env or in_ring else 1
    if not backend:
        print(f"error: no OS keyring here; set the env var {env_name} instead", file=sys.stderr)
        return 1
    if action == "delete":
        print("deleted" if credentials.delete(key) else "nothing stored")
        return 0
    first = getpass.getpass(f"Password for {what}: ")
    if not first or first != getpass.getpass("Again: "):
        print("error: empty or not matching, nothing stored", file=sys.stderr)
        return 1
    credentials.set(key, first)
    print(f"stored in {backend}")
    if os.environ.get(env_name):
        print(f"note: env var {env_name} is also set and takes precedence; remove it")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="callmaibe", description="Let your AI agent phone you.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, hlp in [
        ("daemon", "run the daemon in the foreground"), ("mcp", "run the MCP server"),
        ("status", "show policy, daemon and active call"),
        ("on", "lift the global kill switch"), ("off", "global kill switch: block all calls"),
        ("unsnooze", "clear snooze"), ("hook", "Claude Code UserPromptSubmit hook"),
        ("test-call", "place a test call that echoes what you say"),
    ]:
        sub.add_parser(name, help=hlp)
    sp = sub.add_parser("snooze", help="block calls for a while")
    sp.add_argument("duration", type=parse_duration, help="30m, 2h, 90s")
    from . import setup as setup_wizard
    setup_wizard.add_arguments(sub.add_parser("setup", help="interactive config wizard"))
    se = sub.add_parser("session", help="per-session switch")
    se.add_argument("action", choices=["on", "off", "now", "status"])
    se.add_argument("--session", default=None)
    pw = sub.add_parser("password", help="store the SIP (or TURN) password in the OS keyring")
    pw.add_argument("action", choices=["set", "delete", "status"])
    pw.add_argument("--turn", action="store_true", help="the TURN password instead of the SIP one")
    args = ap.parse_args(argv)

    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return 0
    if args.cmd == "setup":
        return setup_wizard.run(args)
    cfg = config.load()
    if args.cmd == "daemon":
        from .daemon import serve
        serve(cfg)
        return 0
    if args.cmd == "password":
        return cmd_password(cfg, args.action, args.turn)
    policy = Policy(cfg.policy)
    try:
        match args.cmd:
            case "status":
                return cmd_status(cfg, policy)
            case "on" | "off":
                policy.set_global(args.cmd == "on")
                print(f"global switch {args.cmd}")
            case "snooze":
                policy.snooze(args.duration)
                print(f"snoozed until {policy.status(session_key())['snoozed_until']}")
            case "unsnooze":
                policy.clear_snooze()
                print("snooze cleared")
            case "session":
                print(session_action(policy, args.action, args.session or session_key()))
            case "hook":
                return cmd_hook(policy)
            case "test-call":
                return cmd_test_call(cfg, policy)
    except DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
