"""Resampling helpers between engine rates and the internal PCM format."""

import numpy as np
import soxr

from callmaibe.audio import SAMPLE_RATE


def to_internal(samples: np.ndarray, rate: int) -> bytes:
    """float32 in [-1, 1] or int16 samples at `rate` -> internal PCM bytes."""
    x = samples.astype(np.float32) / 32768 if samples.dtype == np.int16 else samples.astype(np.float32)
    if rate != SAMPLE_RATE:
        x = soxr.resample(x, rate, SAMPLE_RATE)
    return np.clip(np.rint(x * 32768), -32768, 32767).astype("<i2").tobytes()


def from_internal(pcm: bytes, rate: int = SAMPLE_RATE) -> np.ndarray:
    """Internal PCM bytes -> float32 samples at `rate`."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    return x if rate == SAMPLE_RATE else soxr.resample(x, SAMPLE_RATE, rate)
