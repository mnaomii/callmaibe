"""Voice activity detection and end-of-turn detection. Implemented by the speech workstream."""

from collections import deque
from enum import StrEnum

import webrtcvad

from callmaibe.audio import FRAME_BYTES, FRAME_MS, SAMPLE_RATE

_WINDOW = 300 // FRAME_MS        # sliding window / pre-roll, in frames
_START_VOICED = 0.6
_TRAIL_KEEP = 200 // FRAME_MS    # trailing silence kept in utterance()


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

    def __init__(self, end_silence_ms: int = 1200, max_turn_s: float = 60, aggressiveness: int = 2):
        self._vad = webrtcvad.Vad(aggressiveness)
        self._end_frames = max(1, end_silence_ms // FRAME_MS)
        self._max_frames = int(max_turn_s * 1000 // FRAME_MS)
        self.reset()

    def _voiced(self, frame: bytes) -> bool:
        return self._vad.is_speech(frame, SAMPLE_RATE)

    def feed(self, frame: bytes) -> TurnEvent:
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"frame must be {FRAME_BYTES} bytes, got {len(frame)}")
        if self._done:
            return TurnEvent.NONE
        voiced = self._voiced(frame)
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
        self._window: deque[bool] = deque(maxlen=_WINDOW)
        self._frames: deque[bytes] = deque(maxlen=_WINDOW)
        self._started = False
        self._done = False
        self._speech_frames = 0
        self._silence = 0
