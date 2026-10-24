"""Local STT via faster-whisper."""

import re
import threading
from collections.abc import Iterable

import numpy as np

from callmaibe import paths
from callmaibe.audio import SAMPLE_RATE, SAMPLE_WIDTH
from callmaibe.audio.resample import from_internal
from callmaibe.config import STTCfg
from callmaibe.stt.base import STT

_MIN_BYTES = int(0.3 * SAMPLE_RATE) * SAMPLE_WIDTH
_WINDOW_S = 30          # Whisper decodes 30 s at a time; max_new_tokens counts per window

# On noise Whisper writes lines from its training data (video subtitles), and it can loop on one
# word ("No, no, no, ..." up to the token limit). clean() undoes both.
_SUBTITLE = re.compile(r"thanks? (you )?(so much )?for watching|please subscribe"
                       r"|like and subscribe|subtitles by|amara\.org", re.IGNORECASE)
_NON_SPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)|♪")              # [BLANK_AUDIO], (music)
_REPEAT = re.compile(r"\b(\w+(?:\W+\w+){0,3}?)(?:\W+\1\b){3,}", re.IGNORECASE)


def _words(text: str) -> str:
    return " " + " ".join(re.findall(r"\w+", text.lower())) + " "


def clean(segments: Iterable[str], prompt: str = "") -> str:
    """Join segment texts without Whisper's usual hallucinations; "" if nothing real is left."""
    text = _NON_SPEECH.sub("", " ".join(segments))
    sentences = re.split(r"(?<=[.!?])\s+", text)
    text = " ".join(s for s in sentences if not _SUBTITLE.search(s))
    text = " ".join(_REPEAT.sub(r"\1", text).split())
    words = _words(text)
    if words in ("  ", " you ") or (words.count(" ") > 4 and words in _words(prompt)):  # noise, echo
        return ""
    return text


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
        seconds = min(len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH), _WINDOW_S)
        segments, _ = self._load().transcribe(
            from_internal(pcm),
            language=self.cfg.language or None,
            initial_prompt=self.cfg.prompt or None,
            vad_filter=False,
            condition_on_previous_text=False,
            beam_size=1,
            temperature=0.0,                        # no retries: each one is another full decode
            max_new_tokens=int(6 * seconds) + 10,   # room for fast speech; a word loop stops early
        )
        return clean((s.text for s in segments), self.cfg.prompt)

    def warmup(self) -> None:
        segments, _ = self._load().transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language=self.cfg.language or None)
        list(segments)
