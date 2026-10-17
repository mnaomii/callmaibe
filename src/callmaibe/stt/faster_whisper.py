"""Local STT via faster-whisper."""

import threading

import numpy as np

from callmaibe import paths
from callmaibe.audio import SAMPLE_RATE, SAMPLE_WIDTH
from callmaibe.audio.resample import from_internal
from callmaibe.config import STTCfg
from callmaibe.stt.base import STT

_MIN_BYTES = int(0.3 * SAMPLE_RATE) * SAMPLE_WIDTH


class FasterWhisperSTT(STT):
    def __init__(self, cfg: STTCfg):
        self.cfg = cfg
        self._model = None
        self._lock = threading.Lock()   # warmup thread and the first call both load the model

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self.cfg.model,
                    device=self.cfg.device,
                    compute_type=self.cfg.compute_type,
                    download_root=str(paths.models_dir() / "whisper"),
                )
            return self._model

    def transcribe(self, pcm: bytes) -> str:
        if len(pcm) < _MIN_BYTES:
            return ""
        segments, _ = self._load().transcribe(
            from_internal(pcm),
            language=self.cfg.language or None,
            vad_filter=False,
            condition_on_previous_text=False,
            beam_size=1,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    def warmup(self) -> None:
        segments, _ = self._load().transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language=self.cfg.language or None)
        list(segments)
