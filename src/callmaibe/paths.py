"""Where callmaibe keeps its files.

Everything lives under one base directory so a daemon in WSL and a CLI/MCP
server on Windows can share it: set CALLMAIBE_HOME on both sides to the same
folder (e.g. /mnt/c/Users/me/AppData/Roaming/callmaibe in WSL).
"""

import os
from pathlib import Path

from platformdirs import user_data_dir


def home() -> Path:
    p = Path(os.environ.get("CALLMAIBE_HOME") or user_data_dir("callmaibe", appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_file() -> Path:
    return home() / "config.toml"


def policy_file() -> Path:
    return home() / "policy.json"


def token_file() -> Path:
    return home() / "daemon.token"


def lock_file() -> Path:
    return home() / "daemon.lock"


def transcripts_dir() -> Path:
    p = home() / "transcripts"
    p.mkdir(exist_ok=True)
    return p


def logs_dir() -> Path:
    p = home() / "logs"
    p.mkdir(exist_ok=True)
    return p


def models_dir() -> Path:
    p = home() / "models"
    p.mkdir(exist_ok=True)
    return p
