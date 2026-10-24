"""Config schema and loader. See docs/PLAN.md for the TOML layout."""

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from . import paths


@dataclass
class AccountCfg:
    uri: str = ""                                   # sip:agent@sip.linphone.org
    username: str = ""                              # auth user; defaults to the uri's user part
    password_env: str = "CALLMAIBE_SIP_PASSWORD"
    transport: str = "tls"                          # tls | tcp | udp
    stun: list[str] = field(default_factory=lambda: ["stun.linphone.org"])
    turn_server: str = ""                           # host:port, empty = no TURN
    turn_username: str = ""
    turn_password_env: str = "CALLMAIBE_TURN_PASSWORD"

    # Env var first (headless/WSL), then the OS keyring (`callmaibe password set`).
    @property
    def password(self) -> str:
        from . import credentials

        return os.environ.get(self.password_env) or credentials.get(credentials.sip_key(self.uri))

    @property
    def turn_password(self) -> str:
        from . import credentials

        key = credentials.turn_key(self.turn_server, self.turn_username)
        return os.environ.get(self.turn_password_env) or credentials.get(key)


@dataclass
class CalleeCfg:
    uri: str = ""                                   # sip:me@sip.linphone.org


@dataclass
class MediaCfg:
    backend: str = "pjsua2"                         # pjsua2 | local | fake
    encryption: str = "srtp"                        # srtp | none
    ice: bool = True
    codecs: list[str] = field(default_factory=lambda: ["G722/16000", "PCMU/8000", "PCMA/8000"])


@dataclass
class TTSCfg:
    engine: str = "kokoro"
    voice: str = "af_heart"
    speed: float = 1.0
    variant: str = "int8"                           # kokoro model: int8 (88 MB) | fp32 (310 MB)


@dataclass
class STTCfg:
    engine: str = "faster-whisper"
    model: str = "base.en"                          # non-English needs a multilingual model: "base" or "small"
    language: str = "en"
    device: str = "auto"                            # auto | cpu | cuda
    compute_type: str = "int8"
    # Tells Whisper what to expect: helps with short replies and jargon. "" turns it off.
    prompt: str = "Phone call with a coding assistant. Yes, no, go ahead, wait."


@dataclass
class ConversationCfg:
    ring_timeout_s: float = 45
    listen_timeout_s: float = 20                    # no speech at all -> status no_reply
    end_silence_ms: int = 900                       # silence that ends your turn
    max_turn_s: float = 60
    idle_hangup_s: float = 120                      # agent silent this long between turns -> hang up
    max_call_min: float = 20
    thinking_cue: bool = True
    keep_transcripts: bool = True
    barge_in: bool = True                           # let you interrupt the agent while it speaks
    barge_in_min_ms: int = 700                      # sustained speech needed to interrupt
    first_turn_grace_s: float = 2.5                 # no barge-in this long into the opening message
    vad_aggressiveness: int = 3                     # webrtcvad 0-3; higher rejects more noise
    debug_audio: bool = False                       # save incoming call audio to <home>/logs/audio/


@dataclass
class QuietRange:
    days: str = "mon-sun"                           # "mon-fri", "sat,sun", "tue"
    start: str = "00:00"                            # TOML key: from
    end: str = "00:00"                              # TOML key: to


@dataclass
class PolicyCfg:
    enabled: bool = True                            # global kill switch default
    decline_snooze_min: float = 30
    once_permit_min: float = 10                     # how long /call now stays valid
    quiet_hours: list[QuietRange] = field(default_factory=list)


@dataclass
class DaemonCfg:
    host: str = "127.0.0.1"
    port: int = 47823
    runner: str = "auto"                            # auto | native | wsl (see runtime.py)
    wsl_distro: str = ""                            # empty = WSL default distro
    wsl_python: str = "~/.venvs/callmaibe/bin/python"


@dataclass
class Config:
    account: AccountCfg = field(default_factory=AccountCfg)
    callee: CalleeCfg = field(default_factory=CalleeCfg)
    media: MediaCfg = field(default_factory=MediaCfg)
    tts: TTSCfg = field(default_factory=TTSCfg)
    stt: STTCfg = field(default_factory=STTCfg)
    conversation: ConversationCfg = field(default_factory=ConversationCfg)
    policy: PolicyCfg = field(default_factory=PolicyCfg)
    daemon: DaemonCfg = field(default_factory=DaemonCfg)


_RENAMES = {"from": "start", "to": "end"}


def _build(cls: type, data: dict[str, Any], where: str):
    known = {f.name: f for f in fields(cls)}
    kwargs = {}
    for key, value in data.items():
        name = _RENAMES.get(key, key) if cls is QuietRange else key
        if name not in known:
            raise ValueError(f"unknown config key {where}.{key}")
        default = known[name].default_factory() if callable(known[name].default_factory) else known[name].default
        if is_dataclass(default):
            value = _build(type(default), value, f"{where}.{key}")
        elif name == "quiet_hours":
            value = [_build(QuietRange, v, f"{where}.{key}") for v in value]
        kwargs[name] = value
    return cls(**kwargs)


def load(path: Path | None = None) -> Config:
    path = path or paths.config_file()
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        return _build(Config, tomllib.load(f), "config")
