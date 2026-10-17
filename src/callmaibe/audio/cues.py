"""Short synthesized sounds in the internal PCM format."""

import numpy as np

from callmaibe.audio import SAMPLE_RATE


def _tone(freq: float, ms: int, volume: float, fade_ms: int = 30) -> np.ndarray:
    n = SAMPLE_RATE * ms // 1000
    t = np.arange(n) / SAMPLE_RATE
    x = np.sin(2 * np.pi * freq * t)
    f = SAMPLE_RATE * fade_ms // 1000
    env = np.ones(n)
    env[:f] = np.linspace(0, 1, f)
    env[-f:] = np.linspace(1, 0, f)
    return x * env * volume


def _pcm(x: np.ndarray) -> bytes:
    return (x * 32767).astype("<i2").tobytes()


def thinking() -> bytes:
    """Soft two-note chime, ~400 ms, quiet."""
    return _pcm(np.concatenate([_tone(660, 200, 0.12, 60), _tone(880, 200, 0.12, 80)]))


def beep() -> bytes:
    """Short 200 ms beep."""
    return _pcm(_tone(1000, 200, 0.25, 15))
