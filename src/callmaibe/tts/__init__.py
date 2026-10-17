from callmaibe.config import TTSCfg
from callmaibe.tts.base import TTS


def create(cfg: TTSCfg) -> TTS:
    if cfg.engine == "kokoro":
        from callmaibe.tts.kokoro import KokoroTTS

        return KokoroTTS(cfg)
    raise ValueError(f"unknown tts engine: {cfg.engine!r}")
