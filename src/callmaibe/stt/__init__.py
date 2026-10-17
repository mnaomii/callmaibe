from callmaibe.config import STTCfg
from callmaibe.stt.base import STT


def create(cfg: STTCfg) -> STT:
    if cfg.engine == "faster-whisper":
        from callmaibe.stt.faster_whisper import FasterWhisperSTT

        return FasterWhisperSTT(cfg)
    raise ValueError(f"unknown stt engine: {cfg.engine!r}")
