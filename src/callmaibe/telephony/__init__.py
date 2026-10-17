"""Telephony backends. `create(cfg)` picks one from `cfg.media.backend`."""

from ..config import Config
from .base import Call, CallState, EndReason, Telephony

__all__ = ["Call", "CallState", "EndReason", "Telephony", "create"]


def create(cfg: Config) -> Telephony:
    backend = cfg.media.backend
    if backend == "pjsua2":
        from .pjsua2 import Pjsua2Telephony
        return Pjsua2Telephony(cfg)
    if backend == "local":
        from .local import LocalTelephony
        return LocalTelephony(cfg)
    if backend == "fake":
        from .fake import FakeTelephony
        return FakeTelephony(speed=1.0)
    raise ValueError(f"unknown media.backend {backend!r} (expected pjsua2, local or fake)")
