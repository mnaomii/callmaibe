"""Voice activity detection and end-of-turn detection. Implemented by the speech workstream."""

from collections import deque
from enum import StrEnum
from math import log10

import numpy as np
import webrtcvad

from callmaibe.audio import FRAME_BYTES, FRAME_MS, SAMPLE_RATE

_WINDOW = 300 // FRAME_MS        # sliding window / pre-roll, in frames
_START_VOICED = 0.6
_TRAIL_KEEP = 200 // FRAME_MS    # trailing silence kept in utterance()
_FLOOR_RISE = 1.03               # noise floor creeps up 3% per frame, drops at once
_MIN_FLOOR = 1.0                 # RMS; keeps the creep alive after digital silence


class TurnEvent(StrEnum):
    NONE = "none"
    SPEECH_START = "speech_start"   # user started talking (also used for barge-in)
    TURN_END = "turn_end"           # speech followed by end_silence_ms of silence, or max_turn_s reached


class TurnDetector:
    """Feed 20 ms frames; get events. Collects the user's speech for STT.

    Usage:
        det = TurnDetector(end_silence_ms=1200, max_turn_s=60)
        for frame in frames:
            ev = det.feed(frame)
            if ev is TurnEvent.TURN_END:
                pcm = det.utterance()   # speech incl. ~300 ms pre-roll, internal format
                break
        det.reset()
    """

    def __init__(self, end_silence_ms: int = 1200, max_turn_s: float = 60, aggressiveness: int = 2,
                 noise_mult: float = 3.0, abs_floor_dbfs: float = -45.0):
        self._vad = webrtcvad.Vad(aggressiveness)
        self._noise_mult = noise_mult
        self._abs_floor = 32768 * 10 ** (abs_floor_dbfs / 20)
        self._floor: float | None = None
        self._end_frames = max(1, end_silence_ms // FRAME_MS)
        self._max_frames = int(max_turn_s * 1000 // FRAME_MS)
        self.reset()

    def _voiced(self, frame: bytes) -> bool:
        """webrtcvad says speech AND the frame is clearly louder than the tracked noise floor."""
        x = np.frombuffer(frame, dtype="<i2").astype(np.float64)
        rms = float(np.sqrt(np.mean(x * x)))
        if self._floor is None or rms < self._floor:
            self._floor = max(rms, _MIN_FLOOR)
        else:
            self._floor *= _FLOOR_RISE
        gate = max(self._floor * self._noise_mult, self._abs_floor)
        return rms >= gate and self._vad.is_speech(frame, SAMPLE_RATE)

    @property
    def noise_floor_db(self) -> float | None:
        """Tracked noise floor in dBFS (None until a frame went through the energy gate)."""
        return None if self._floor is None else 20 * log10(self._floor / 32768)

    def feed(self, frame: bytes) -> TurnEvent:
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"frame must be {FRAME_BYTES} bytes, got {len(frame)}")
        if self._done:
            return TurnEvent.NONE
        voiced = self.last_voiced = self._voiced(frame)
        if not self._started:
            self._window.append(voiced)
            self._frames.append(frame)   # bounded deque: acts as pre-roll
            if len(self._window) == _WINDOW and sum(self._window) >= _START_VOICED * _WINDOW:
                self._started = True
                self._frames = deque(self._frames)   # unbounded from here
                self._speech_frames = len(self._frames)
                self._silence = 0
                return TurnEvent.SPEECH_START
            return TurnEvent.NONE
        self._frames.append(frame)
        self._speech_frames += 1
        self._silence = 0 if voiced else self._silence + 1
        if self._silence >= self._end_frames or self._speech_frames >= self._max_frames:
            self._done = True
            return TurnEvent.TURN_END
        return TurnEvent.NONE

    @property
    def in_speech(self) -> bool:
        return self._started and not self._done

    def utterance(self) -> bytes:
        frames = list(self._frames)
        if self._silence > _TRAIL_KEEP:
            frames = frames[: len(frames) - (self._silence - _TRAIL_KEEP)]
        return b"".join(frames)

    def reset(self) -> None:
        self.last_voiced = False         # did the latest frame count as voiced (after the energy gate)
        self._window: deque[bool] = deque(maxlen=_WINDOW)
        self._frames: deque[bytes] = deque(maxlen=_WINDOW)
        self._started = False
        self._done = False
        self._speech_frames = 0
        self._silence = 0
