"""Scriptable in-memory telephony backend for tests.

A FakeCall models a phone line with a virtual clock that advances one 20 ms
frame per `read()`. Outgoing audio drains in lock-step with reads, the way a
real call sends one frame out for every frame that comes in. `speed` sets how
fast frames are paced relative to real time: 1.0 is real time, 100 is a
hundred times faster, `math.inf` never sleeps.

    tel = FakeTelephony(speed=100)
    tel.script(answer="answer", answer_after_s=2,
               incoming=[0.5, speech_pcm, 3.0], hangup_at_s=None)
    call = tel.dial("sip:me@example.org")

`incoming` segments are PCM bytes or a number of seconds of silence. Once the
script runs out the line delivers silence. All `hangup_at_s`, `answer_after_s`
values are virtual seconds (measured from answer for `hangup_at_s`).
"""

import math
import threading
import time
from collections.abc import Iterable

from ..audio import FRAME_BYTES, FRAME_MS, SAMPLE_RATE, SAMPLE_WIDTH
from .base import Call, CallState, EndReason, Telephony

FRAME_S = FRAME_MS / 1000
SILENCE = bytes(FRAME_BYTES)

_OUTCOMES = {
    "decline": EndReason.DECLINED,
    "busy": EndReason.BUSY,
    "never": EndReason.NO_ANSWER,
    "error": EndReason.ERROR,
}


def silence(seconds: float) -> bytes:
    n = round(seconds * SAMPLE_RATE) * SAMPLE_WIDTH
    return bytes(n)


class FakeCall(Call):
    def __init__(
        self,
        uri: str,
        *,
        answer: str = "answer",            # answer | decline | busy | never | error
        answer_after_s: float = 0.0,
        incoming: Iterable[bytes | float] = (),
        hangup_at_s: float | None = None,
        speed: float = 100.0,
        full_duplex: bool = True,
        error_detail: str = "500 fake error",
    ):
        if answer != "answer" and answer not in _OUTCOMES:
            raise ValueError(f"unknown answer behaviour {answer!r}")
        self.uri = uri
        self.full_duplex = full_duplex
        self.end_detail = ""
        self._answer = answer
        self._answer_after = answer_after_s
        self._hangup_at = hangup_at_s
        self._speed = speed
        self._error_detail = error_detail
        self._incoming = bytearray()
        for seg in incoming:
            self._incoming += seg if isinstance(seg, bytes | bytearray) else silence(seg)
        if len(self._incoming) % 2:
            self._incoming.append(0)
        self._pos = 0
        self._cv = threading.Condition()
        self._state = CallState.RINGING
        self._end: EndReason | None = None
        self._out = bytearray()
        self._last_due = 0.0
        self.frames_read = 0             # virtual clock, in frames since answer
        self.played = bytearray()        # audio actually sent to the callee
        self.queued_bytes = 0            # everything ever passed to play()
        self.stop_count = 0

    # --- helpers -------------------------------------------------------
    @property
    def virtual_s(self) -> float:
        return self.frames_read * FRAME_S

    def _real(self, virtual_s: float) -> float:
        return 0.0 if math.isinf(self._speed) else virtual_s / self._speed

    def _finish(self, reason: EndReason, detail: str = "") -> None:
        # caller holds self._cv
        if self._state is CallState.ENDED:
            return
        self._state = CallState.ENDED
        self._end = reason
        self.end_detail = detail
        self._cv.notify_all()

    # --- Call API ------------------------------------------------------
    @property
    def state(self) -> CallState:
        return self._state

    @property
    def end_reason(self) -> EndReason | None:
        return self._end

    def wait_answered(self, timeout_s: float) -> CallState:
        with self._cv:
            if self._state is not CallState.RINGING:
                return self._state
            if self._answer == "answer" and self._answer_after <= timeout_s:
                delay, result = self._answer_after, None
            elif self._answer in ("answer", "never"):
                delay, result = timeout_s, EndReason.NO_ANSWER
            else:
                delay, result = min(self._answer_after, timeout_s), _OUTCOMES[self._answer]
            self._cv.wait_for(lambda: self._state is CallState.ENDED, self._real(delay))
            if self._state is CallState.ENDED:
                return self._state
            if result is None:
                self._state = CallState.ACTIVE
                self._last_due = time.monotonic()
                self._cv.notify_all()
            else:
                self._finish(result, self._error_detail if result is EndReason.ERROR else "")
            return self._state

    def play(self, pcm: bytes) -> None:
        with self._cv:
            if self._state is not CallState.ENDED:
                self._out += pcm
                self.queued_bytes += len(pcm)

    def stop_playback(self) -> None:
        with self._cv:
            self._out.clear()
            self.stop_count += 1

    def playing(self) -> bool:
        return bool(self._out)

    def read(self, timeout_s: float) -> bytes | None:
        deadline = time.monotonic() + timeout_s
        with self._cv:
            while True:
                now = time.monotonic()
                if self._state is CallState.ENDED:
                    return None
                if self._state is CallState.ACTIVE:
                    due = self._last_due + self._real(FRAME_S)
                    if now >= due:
                        break
                    wake = min(due, deadline)
                else:
                    wake = deadline
                if now >= deadline:
                    return None
                self._cv.wait(wake - now)
            # pace from the previous frame, but never build up a large backlog
            self._last_due = due if now - due < 0.05 else now
            if self._hangup_at is not None and self.virtual_s >= self._hangup_at:
                self._finish(EndReason.REMOTE_HANGUP)
                return None
            self.frames_read += 1
            frame = bytes(self._incoming[self._pos:self._pos + FRAME_BYTES])
            self._pos += len(frame)
            if len(frame) < FRAME_BYTES:
                frame += SILENCE[len(frame):]
            sent = self._out[:FRAME_BYTES]
            del self._out[:FRAME_BYTES]
            self.played += sent
            return frame

    def hangup(self) -> None:
        with self._cv:
            self._finish(EndReason.LOCAL_HANGUP)

    # --- test helpers --------------------------------------------------
    def remote_hangup(self) -> None:
        with self._cv:
            self._finish(EndReason.REMOTE_HANGUP)


class FakeTelephony(Telephony):
    """Keyword arguments become the default FakeCall script; `script()` overrides the next dial."""

    def __init__(self, **defaults):
        self._defaults = defaults
        self._next: list[dict] = []
        self._registered = False
        self.calls: list[FakeCall] = []

    def script(self, **kwargs) -> None:
        self._next.append(kwargs)

    def start(self) -> None:
        self._registered = True

    def stop(self) -> None:
        self._registered = False
        for c in self.calls:
            c.hangup()

    @property
    def registered(self) -> bool:
        return self._registered

    def dial(self, uri: str) -> FakeCall:
        kwargs = {**self._defaults, **(self._next.pop(0) if self._next else {})}
        call = FakeCall(uri, **kwargs)
        self.calls.append(call)
        return call

    @property
    def last_call(self) -> FakeCall | None:
        return self.calls[-1] if self.calls else None
