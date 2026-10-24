"""Kokoro TTS via kokoro-onnx. Models are downloaded on first use."""

import logging
import os
import re
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from callmaibe import paths
from callmaibe.audio.resample import to_internal
from callmaibe.config import TTSCfg
from callmaibe.tts.base import TTS

log = logging.getLogger(__name__)

_BASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
_VOICES = "voices-v1.0.bin"
_MAX_SENTENCE = 200

_URL = re.compile(r"https?://\S+|www\.\S+")
_MARKUP = re.compile(r"[*`#>]|^\s*-\s+", re.MULTILINE)


def clean(text: str) -> str:
    text = _URL.sub("link", text)
    text = _MARKUP.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


_FIRST_CHUNK_MAX = 60      # a longer first sentence is split at its first natural break
_FIRST_CHUNK_MIN = 15      # ...but not before this many characters


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", clean(text)):
        if len(sentence) <= _MAX_SENTENCE:
            out.append(sentence)
            continue
        cur = ""
        for part in re.split(r"(?<=,)\s+", sentence):
            if cur and len(cur) + len(part) >= _MAX_SENTENCE:
                out.append(cur)
                cur = part
            else:
                cur = f"{cur} {part}".strip()
        out.append(cur)
    out = [s for s in out if re.search(r"\w", s)]
    if out and len(out[0]) > _FIRST_CHUNK_MAX:       # short first chunk: audio starts sooner
        m = re.search(r"[,;:]\s|\s[-–—]+\s", out[0][_FIRST_CHUNK_MIN:])
        if m:
            cut = _FIRST_CHUNK_MIN + m.start() + (1 if m.group()[0] in ",;:" else 0)
            head, tail = out[0][:cut].strip(), out[0][cut:].strip(" -–—")
            if re.search(r"\w", tail):
                out[0:1] = [head, tail]
    return out


def _fetch(name: str, dest: Path) -> Path:
    path = dest / name
    if path.exists():
        return path
    log.info("downloading %s", name)
    part = path.with_name(f"{name}.{os.getpid()}.part")   # unique, in case two processes download
    urllib.request.urlretrieve(_BASE_URL + name, part)
    os.replace(part, path)
    return path


class _StoppableSession:
    """kokoro-onnx's onnxruntime session, run with our options so stop() can end a sentence."""

    def __init__(self, sess):
        self._sess = sess
        self.options = None

    def run(self, output_names, input_feed, run_options=None):
        return self._sess.run(output_names, input_feed, self.options)

    def __getattr__(self, name):
        return getattr(self._sess, name)


class KokoroTTS(TTS):
    def __init__(self, cfg: TTSCfg):
        self.cfg = cfg
        self._model = None
        self._lock = threading.Lock()   # warmup thread and the first call both load the model

    def _load(self):
        with self._lock:
            return self._load_locked()

    def _load_locked(self):
        if self._model is None:
            from kokoro_onnx import Kokoro

            dest = paths.models_dir() / "kokoro"
            dest.mkdir(exist_ok=True)
            onnx = "kokoro-v1.0.int8.onnx" if self.cfg.variant == "int8" else "kokoro-v1.0.onnx"
            model = Kokoro(str(_fetch(onnx, dest)), str(_fetch(_VOICES, dest)))
            model.sess = _StoppableSession(model.sess)
            self._model = model
        return self._model

    def synthesize(self, text: str) -> Iterator[bytes]:
        from onnxruntime import RunOptions

        model = self._load()
        options = model.sess.options = RunOptions()    # a fresh stop flag for each message
        for sentence in split_sentences(text):
            try:
                samples, rate = model.create(sentence, voice=self.cfg.voice, speed=self.cfg.speed, lang="en-us")
            except Exception:
                if options.terminate:                  # stop() ended this sentence
                    return
                raise
            yield to_internal(samples, rate)

    def stop(self) -> None:
        # A long sentence takes seconds to synthesize; ending it frees the CPU for STT.
        options = self._model.sess.options if self._model is not None else None
        if options is not None:
            options.terminate = True

    def warmup(self) -> None:
        for _ in self.synthesize("Hello."):
            pass
