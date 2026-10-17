"""Conversation engine: owns at most one call and runs the speak/listen turns.

Interface used by the daemon:

    mgr = CallManager(telephony, tts, stt, cfg)
    mgr.start(message, topic=None) -> TurnResult     # dial, ring, speak, listen
    mgr.say(call_id, message, expect_reply=True) -> TurnResult
    mgr.end(call_id, message=None) -> TurnResult
    mgr.active() -> dict | None                      # {"call_id", "state", "started_at", "turns"}
    mgr.close()

All methods block until their turn finishes and are safe to call from
different HTTP handler threads (one call at a time; a second start returns
Status.LINE_BUSY). Policy is NOT checked here; the daemon does that first.

One turn = speak + listen. TTS sentences are queued on the call as soon as
each is synthesized (a producer thread), while this thread keeps reading
incoming frames into a TurnDetector so the user can barge in. Between turns a
background thread plays the thinking cue, drains incoming audio, notices a
remote hangup and enforces idle_hangup_s / max_call_min.
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paths
from .audio import FRAME_MS
from .audio.vad import TurnEvent
from .config import Config
from .status import Status, TurnResult
from .stt.base import STT
from .telephony.base import Call, CallState, EndReason, Telephony
from .tts.base import TTS

log = logging.getLogger(__name__)

FRAME_S = FRAME_MS / 1000
GOODBYE_IDLE = "I have to go now. I'll write the rest in the terminal."
CUE_INTERVAL_S = 4.0
FIRST_CUE_S = 1.0
BARGE_GRACE_S = 0.4          # ignore the first moment of our own playback (line echo, "hello?")
READ_POLL_S = 0.05

_RING_END = {
    EndReason.DECLINED: Status.DECLINED,
    EndReason.BUSY: Status.BUSY,
    EndReason.NO_ANSWER: Status.NO_ANSWER,
}


@dataclass
class _Session:
    call_id: str
    topic: str | None
    started_at: datetime = field(default_factory=datetime.now)
    call: Call | None = None
    answered_mono: float = 0.0
    turns: int = 0
    lines: list[tuple[datetime, str, str]] = field(default_factory=list)
    op_lock: threading.Lock = field(default_factory=threading.Lock)
    idle_thread: threading.Thread | None = None
    idle_stop: threading.Event = field(default_factory=threading.Event)
    producer: threading.Thread | None = None
    done: bool = False

    def note(self, who: str, text: str) -> None:
        if text:
            self.lines.append((datetime.now(), who, text))


class _CallEnded(Exception):
    pass


class CallManager:
    def __init__(
        self,
        telephony: Telephony,
        tts: TTS,
        stt: STT,
        cfg: Config,
        *,
        detector_factory: Callable[[], object] | None = None,
        cue: Callable[[], bytes] | None = None,
        cue_interval_s: float = CUE_INTERVAL_S,
    ):
        self._tel = telephony
        self._tts = tts
        self._stt = stt
        self._cfg = cfg
        self._detector_factory = detector_factory or self._default_detector
        self._cue_fn = cue
        self._cue_pcm: bytes | None = None
        self._cue_interval = cue_interval_s
        self._lock = threading.Lock()
        self._active: _Session | None = None
        self._finished: dict[str, TurnResult] = {}   # results noticed between turns

    # ------------------------------------------------------------------ public API
    def start(self, message: str, topic: str | None = None) -> TurnResult:
        with self._lock:
            if self._active is not None:
                return TurnResult(Status.LINE_BUSY, reason="another call is already active")
            sess = _Session(uuid.uuid4().hex[:8], topic)
            self._active = sess
        with sess.op_lock:
            try:
                uri = self._cfg.callee.uri
                if not uri:
                    raise RuntimeError("callee.uri is not configured")
                log.info("call %s: dialing %s", sess.call_id, uri)
                sess.call = self._tel.dial(uri)
                state = sess.call.wait_answered(self._cfg.conversation.ring_timeout_s)
                if state is not CallState.ACTIVE:
                    return self._not_answered(sess)
                sess.answered_mono = time.monotonic()
                log.info("call %s: answered", sess.call_id)
                return self._turn(sess, message, expect_reply=True)
            except Exception as e:  # noqa: BLE001 - any failure becomes ERROR
                return self._fail(sess, e)

    def say(self, call_id: str, message: str, expect_reply: bool = True) -> TurnResult:
        sess = self._lookup(call_id)
        if sess is None:
            return self._stale(call_id)
        with sess.op_lock:
            if (early := self._resume(sess)) is not None:
                return early
            try:
                return self._turn(sess, message, expect_reply)
            except Exception as e:  # noqa: BLE001 - any failure becomes ERROR
                return self._fail(sess, e)

    def end(self, call_id: str, message: str | None = None) -> TurnResult:
        sess = self._lookup(call_id)
        if sess is None:
            return self._stale(call_id)
        with sess.op_lock:
            if (early := self._resume(sess)) is not None:
                return early
            try:
                if message:
                    sess.note("Agent", message)
                    self._speak(sess, message, barge=False)
                return self._finish(sess, TurnResult(Status.ENDED, call_id=sess.call_id))
            except _CallEnded:
                return self._finish(sess, TurnResult(Status.ENDED_BY_USER, call_id=sess.call_id))
            except Exception as e:  # noqa: BLE001 - any failure becomes ERROR
                return self._fail(sess, e)

    def active(self) -> dict | None:
        with self._lock:
            sess = self._active
            if sess is None:
                return None
            return {
                "call_id": sess.call_id,
                "state": str(sess.call.state) if sess.call else str(CallState.DIALING),
                "started_at": sess.started_at.isoformat(timespec="seconds"),
                "turns": sess.turns,
            }

    def close(self) -> None:
        with self._lock:
            sess = self._active
        if sess is None:
            return
        self._stop_idle(sess)
        with sess.op_lock:
            if not sess.done:
                self._finish(sess, TurnResult(Status.ENDED, call_id=sess.call_id,
                                              reason="daemon shutting down"))

    # ------------------------------------------------------------------ session bookkeeping
    def _lookup(self, call_id: str) -> _Session | None:
        with self._lock:
            sess = self._active
            return sess if sess is not None and sess.call_id == call_id else None

    def _stale(self, call_id: str) -> TurnResult:
        with self._lock:
            remembered = self._finished.pop(call_id, None)
        return remembered or TurnResult(Status.NOT_FOUND, call_id=call_id,
                                        reason="no active call with this id")

    def _resume(self, sess: _Session) -> TurnResult | None:
        """Called with op_lock held: stop the between-turns thread; return its result if it ended the call."""
        self._stop_idle(sess)
        if sess.done:
            return self._stale(sess.call_id)
        return None

    def _not_answered(self, sess: _Session) -> TurnResult:
        call = sess.call
        reason = call.end_reason if call.state is CallState.ENDED else EndReason.NO_ANSWER
        if call.state is not CallState.ENDED:
            call.hangup()
        status = _RING_END.get(reason, Status.ERROR)
        detail = "" if status is not Status.ERROR else (getattr(call, "end_detail", "") or str(reason))
        log.info("call %s: not answered (%s %s)", sess.call_id, reason, detail)
        return self._finish(sess, TurnResult(status, call_id=sess.call_id, reason=detail),
                            transcript=False)

    def _finish(self, sess: _Session, result: TurnResult, transcript: bool = True) -> TurnResult:
        """Hang up, write the transcript and release the line. Idempotent per session."""
        if sess.call is not None:
            try:
                sess.call.hangup()
            except Exception:
                log.exception("call %s: hangup failed", sess.call_id)
        if transcript and not sess.done and self._cfg.conversation.keep_transcripts:
            try:
                result.transcript_path = str(self._write_transcript(sess, result))
            except Exception:
                log.exception("call %s: could not write transcript", sess.call_id)
        sess.done = True
        with self._lock:
            if self._active is sess:
                self._active = None
        log.info("call %s: finished with %s %s", sess.call_id, result.status, result.reason)
        return result

    def _fail(self, sess: _Session, exc: BaseException) -> TurnResult:
        log.error("call %s: error", sess.call_id, exc_info=exc)
        return self._finish(sess, TurnResult(Status.ERROR, call_id=sess.call_id,
                                             reason=f"{type(exc).__name__}: {exc}"),
                            transcript=sess.answered_mono > 0)

    # ------------------------------------------------------------------ one turn
    def _turn(self, sess: _Session, message: str, expect_reply: bool) -> TurnResult:
        call = sess.call
        sess.turns += 1
        sess.note("Agent", message)
        det = self._detector_factory() if expect_reply else None
        try:
            barged = self._speak(sess, message, barge=expect_reply and call.full_duplex, det=det)
            if barged:
                sess.lines[-1] = (*sess.lines[-1][:2], message + " [interrupted]")
            if not expect_reply:
                return self._between_turns(sess, TurnResult(Status.IN_CALL, call_id=sess.call_id))
            text = self._listen(sess, det, speech_started=barged)
        except _CallEnded as e:
            text = e.args[0] if e.args else ""
            sess.note("You", text)
            return self._finish(sess, TurnResult(Status.ENDED_BY_USER, user_said=text,
                                                 call_id=sess.call_id))
        if text is None:
            return self._between_turns(sess, TurnResult(Status.NO_REPLY, call_id=sess.call_id))
        sess.note("You", text)
        return self._between_turns(sess, TurnResult(Status.IN_CALL, user_said=text,
                                                    call_id=sess.call_id))

    def _speak(self, sess: _Session, text: str, barge: bool, det=None) -> bool:
        """Play `text`, streaming TTS sentences as they come. Returns True on barge-in.

        Raises _CallEnded (with the partial reply if the user was talking) if the call drops.
        """
        call = sess.call
        if sess.producer is not None:            # a cancelled synthesis from an earlier turn
            sess.producer.join()
        cancel = threading.Event()
        done = threading.Event()
        plock = threading.Lock()
        errors: list[BaseException] = []

        def produce():
            try:
                for chunk in self._tts.synthesize(text):
                    with plock:
                        if cancel.is_set():
                            break
                        call.play(chunk)
            except BaseException as e:  # noqa: BLE001 - re-raised in the turn thread
                errors.append(e)
            finally:
                done.set()

        sess.producer = threading.Thread(target=produce, name="callmaibe-tts", daemon=True)
        sess.producer.start()
        played_frames = 0
        armed = False
        try:
            while True:
                if errors:
                    raise errors[0]
                frame = call.read(READ_POLL_S)
                if frame is None:
                    if call.state is CallState.ENDED:
                        raise _CallEnded(self._partial(det) if armed else "")
                elif barge and not armed:
                    if call.playing():
                        played_frames += 1
                        armed = played_frames * FRAME_S >= BARGE_GRACE_S
                elif barge and det.feed(frame) is TurnEvent.SPEECH_START:
                    with plock:
                        cancel.set()
                        call.stop_playback()
                    log.info("call %s: barge-in", sess.call_id)
                    return True
                if done.is_set() and not call.playing():
                    if errors:
                        raise errors[0]
                    return False
        finally:
            cancel.set()

    def _listen(self, sess: _Session, det, speech_started: bool) -> str | None:
        """Listen until TURN_END; return the transcript, or None if nobody spoke in time."""
        call = sess.call
        c = self._cfg.conversation
        t0, frames = time.monotonic(), 0
        while True:
            frame = call.read(READ_POLL_S * 2)
            if frame is None:
                if call.state is CallState.ENDED:
                    raise _CallEnded(self._partial(det) if speech_started else "")
            else:
                frames += 1
                ev = det.feed(frame)
                if ev is TurnEvent.SPEECH_START:
                    speech_started = True
                elif ev is TurnEvent.TURN_END:
                    text = self._stt.transcribe(det.utterance())
                    if text:
                        return text
                    log.info("call %s: empty transcription, still listening", sess.call_id)
                    det.reset()
                    speech_started = False
                    t0, frames = time.monotonic(), 0
                    continue
            # frames arrive in real time on a real line; the frame count also keeps fast fakes honest
            elapsed = max(time.monotonic() - t0, frames * FRAME_S)
            if not speech_started and elapsed >= c.listen_timeout_s:
                return None
            if speech_started and elapsed >= c.listen_timeout_s + c.max_turn_s + 5:
                log.warning("call %s: detector never ended the turn; forcing it", sess.call_id)
                return self._stt.transcribe(det.utterance())

    def _partial(self, det) -> str:
        if det is None:
            return ""
        pcm = det.utterance()
        return self._stt.transcribe(pcm) if pcm else ""

    # ------------------------------------------------------------------ between turns
    def _between_turns(self, sess: _Session, result: TurnResult) -> TurnResult:
        sess.idle_stop = threading.Event()
        sess.idle_thread = threading.Thread(target=self._idle, args=(sess, sess.idle_stop),
                                            name="callmaibe-idle", daemon=True)
        sess.idle_thread.start()
        return result

    def _stop_idle(self, sess: _Session) -> None:
        t = sess.idle_thread
        if t is not None:
            sess.idle_stop.set()
            if t is not threading.current_thread():
                t.join()
            sess.idle_thread = None

    def _cue(self) -> bytes | None:
        if not self._cfg.conversation.thinking_cue:
            return None
        if self._cue_pcm is None:
            try:
                fn = self._cue_fn
                if fn is None:
                    from .audio.cues import thinking as fn
                self._cue_pcm = fn() or b""
            except Exception:
                log.warning("thinking cue unavailable", exc_info=True)
                self._cue_pcm = b""
        return self._cue_pcm or None

    def _idle(self, sess: _Session, stop: threading.Event) -> None:
        call = sess.call
        c = self._cfg.conversation
        start = time.monotonic()
        idle_deadline = start + c.idle_hangup_s
        max_deadline = sess.answered_mono + c.max_call_min * 60
        next_cue = start + FIRST_CUE_S
        cue = self._cue()
        try:
            while not stop.is_set():
                call.read(READ_POLL_S * 2)          # drain and discard
                if call.state is CallState.ENDED:
                    log.info("call %s: remote hangup between turns", sess.call_id)
                    self._remember(sess, TurnResult(Status.ENDED_BY_USER, call_id=sess.call_id,
                                                    reason="you hung up"))
                    return
                now = time.monotonic()
                if now >= idle_deadline or now >= max_deadline:
                    reason = (f"idle for {c.idle_hangup_s:g} s without call_say"
                              if now >= idle_deadline else
                              f"maximum call length of {c.max_call_min:g} min reached")
                    log.info("call %s: hanging up: %s", sess.call_id, reason)
                    call.stop_playback()
                    sess.note("Agent", GOODBYE_IDLE)
                    try:
                        self._speak(sess, GOODBYE_IDLE, barge=False)
                    except _CallEnded:
                        pass
                    self._remember(sess, TurnResult(Status.ENDED, call_id=sess.call_id,
                                                    reason=reason))
                    return
                if cue and now >= next_cue:
                    if not call.playing():
                        call.play(cue)
                    next_cue = now + self._cue_interval
            if call.playing():                        # cut a cue short; the agent speaks now
                call.stop_playback()
        except Exception as e:
            log.exception("call %s: error between turns", sess.call_id)
            self._remember(sess, TurnResult(Status.ERROR, call_id=sess.call_id,
                                            reason=f"{type(e).__name__}: {e}"))

    def _remember(self, sess: _Session, result: TurnResult) -> None:
        result = self._finish(sess, result)
        with self._lock:
            self._finished[sess.call_id] = result
            while len(self._finished) > 16:
                self._finished.pop(next(iter(self._finished)))

    # ------------------------------------------------------------------ transcripts
    def _write_transcript(self, sess: _Session, result: TurnResult) -> Path:
        stamp = sess.started_at.strftime("%Y%m%d-%H%M%S")
        path = paths.transcripts_dir() / f"{stamp}-{sess.call_id}.md"
        out = [f"# Call {sess.call_id}", ""]
        if sess.topic:
            out.append(f"Topic: {sess.topic}")
        out.append(f"Started: {sess.started_at.isoformat(timespec='seconds')}")
        ended = f"Ended: {datetime.now().isoformat(timespec='seconds')} ({result.status}"
        out.append(ended + (f": {result.reason})" if result.reason else ")"))
        out.append("")
        for when, who, text in sess.lines:
            out.append(f"- [{when:%H:%M:%S}] {who}: {text}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return path

    # ------------------------------------------------------------------ defaults
    def _default_detector(self):
        from .audio.vad import TurnDetector
        c = self._cfg.conversation
        return TurnDetector(end_silence_ms=c.end_silence_ms, max_turn_s=c.max_turn_s)
