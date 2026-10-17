"""Local mic/speaker "telephony" for trying the whole loop on a laptop without SIP.

`dial` plays a short double beep and "answers" at once. The speaker is heard
by the mic, so `full_duplex` is False and the engine skips barge-in.
"""

import logging
import queue
import threading
import time

import numpy as np

from ..audio import FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE
from ..config import Config
from .base import Call, CallState, EndReason, Telephony

log = logging.getLogger(__name__)

ECHO_TAIL_S = 0.25        # speaker latency + room echo after the last queued sample


def _ring() -> bytes:
    t = np.arange(int(0.18 * SAMPLE_RATE)) / SAMPLE_RATE
    beep = 0.25 * np.sin(2 * np.pi * 880 * t) * np.minimum(1, np.minimum(t, t[::-1]) / 0.01)
    gap = np.zeros(int(0.12 * SAMPLE_RATE))
    tail = np.zeros(int(0.3 * SAMPLE_RATE))
    return (np.concatenate([beep, gap, beep, tail]) * 32767).astype("<i2").tobytes()


class LocalCall(Call):
    full_duplex = False

    def __init__(self):
        import sounddevice as sd

        self._lock = threading.Lock()
        self._out = bytearray()
        self._last_out = 0.0
        self._in: queue.Queue[bytes] = queue.Queue(maxsize=250)   # 5 s
        self._state = CallState.ACTIVE
        self._end: EndReason | None = None
        self._stream = sd.RawStream(samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES, channels=1,
                                    dtype="int16", callback=self._callback, latency="low")
        self._stream.start()

    def _callback(self, indata, outdata, frames, time_info, status):
        if status:
            log.debug("sounddevice status: %s", status)
        data = bytes(indata)
        try:
            self._in.put_nowait(data)
        except queue.Full:                      # nobody reading: drop the oldest frame
            try:
                self._in.get_nowait()
            except queue.Empty:
                pass
            self._in.put_nowait(data)
        n = len(outdata)
        with self._lock:
            chunk = bytes(self._out[:n])
            del self._out[:n]
            if chunk:
                self._last_out = time.monotonic()
        outdata[:len(chunk)] = chunk
        if len(chunk) < n:
            outdata[len(chunk):] = bytes(n - len(chunk))

    @property
    def state(self) -> CallState:
        return self._state

    @property
    def end_reason(self) -> EndReason | None:
        return self._end

    def wait_answered(self, timeout_s: float) -> CallState:
        return self._state

    def play(self, pcm: bytes) -> None:
        with self._lock:
            if self._state is CallState.ACTIVE:
                self._out += pcm

    def stop_playback(self) -> None:
        with self._lock:
            self._out.clear()

    def playing(self) -> bool:
        with self._lock:
            return bool(self._out) or time.monotonic() - self._last_out < ECHO_TAIL_S

    def read(self, timeout_s: float) -> bytes | None:
        if self._state is CallState.ENDED:
            return None
        try:
            frame = self._in.get(timeout=timeout_s)
        except queue.Empty:
            return None
        if self._state is CallState.ENDED:
            return None
        return frame if len(frame) == FRAME_BYTES else frame[:FRAME_BYTES].ljust(FRAME_BYTES, b"\0")

    def hangup(self) -> None:
        with self._lock:
            if self._state is CallState.ENDED:
                return
            self._state = CallState.ENDED
            self._end = EndReason.LOCAL_HANGUP
            self._out.clear()
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            log.exception("closing audio streams failed")


class LocalTelephony(Telephony):
    def __init__(self, cfg: Config | None = None):
        self._cfg = cfg
        self._started = False

    def start(self) -> None:
        import sounddevice as sd

        sd.check_input_settings(channels=1, dtype="int16", samplerate=SAMPLE_RATE)
        sd.check_output_settings(channels=1, dtype="int16", samplerate=SAMPLE_RATE)
        log.info("local audio: input %s, output %s",
                 sd.query_devices(kind="input")["name"], sd.query_devices(kind="output")["name"])
        self._started = True

    def stop(self) -> None:
        self._started = False

    @property
    def registered(self) -> bool:
        return self._started

    def dial(self, uri: str) -> LocalCall:
        log.info("local call (pretending to dial %s)", uri)
        call = LocalCall()
        call.play(_ring())
        return call
