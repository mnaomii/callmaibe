"""Decides where the daemon runs: natively, or inside WSL on Windows.

The pjsua2 Windows wheels have no TLS, so on Windows the SIP daemon runs in
WSL while the MCP server, CLI and hooks stay native. Everything else (Linux,
macOS, and the `local`/`fake` backends on Windows) runs natively.

runner = "auto"   -> WSL on Windows when media.backend is pjsua2, else native
runner = "native" -> always native
runner = "wsl"    -> always WSL (Windows only)

Both sides share one data directory: the WSL daemon gets CALLMAIBE_HOME set
to the /mnt/ path of the Windows-side home, and SIP/TURN passwords cross the
boundary through WSLENV.
"""

import os
import shlex
import sys
from pathlib import PureWindowsPath

from . import paths
from .config import Config


def uses_wsl(cfg: Config) -> bool:
    if sys.platform != "win32" or cfg.daemon.runner == "native":
        return False
    return cfg.daemon.runner == "wsl" or cfg.media.backend == "pjsua2"


def to_wsl_path(path: str) -> str:
    """C:\\Users\\me\\x -> /mnt/c/Users/me/x (default automount root)."""
    p = PureWindowsPath(path)
    return f"/mnt/{p.drive.rstrip(':').lower()}/" + "/".join(p.parts[1:])


def daemon_command(cfg: Config) -> list[str]:
    if override := os.environ.get("CALLMAIBE_DAEMON_CMD"):
        return shlex.split(override, posix=sys.platform != "win32")
    if not uses_wsl(cfg):
        return [sys.executable, "-m", "callmaibe", "daemon"]
    python = cfg.daemon.wsl_python
    if python.startswith("~/"):
        python = "$HOME/" + python[2:]
    script = f'CALLMAIBE_HOME="$1" exec {python} -m callmaibe daemon'
    distro = ["-d", cfg.daemon.wsl_distro] if cfg.daemon.wsl_distro else []
    return ["wsl.exe", *distro, "--exec", "sh", "-c", script, "sh", to_wsl_path(str(paths.home()))]


def daemon_env(cfg: Config) -> dict[str, str]:
    """WSL has no keyring, so passwords read on the Windows side go to the daemon process only."""
    env = dict(os.environ)
    if uses_wsl(cfg):
        secrets = {cfg.account.password_env: cfg.account.password,
                   cfg.account.turn_password_env: cfg.account.turn_password}
        env.update({name: value for name, value in secrets.items() if value})
        env["WSLENV"] = ":".join(filter(None, [env.get("WSLENV", ""), *secrets]))
    return env


def describe(cfg: Config) -> str:
    if not uses_wsl(cfg):
        return "native"
    return f"wsl ({cfg.daemon.wsl_distro or 'default distro'})"
