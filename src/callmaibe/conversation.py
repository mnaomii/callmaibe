"""Conversation engine: owns at most one call and runs the speak/listen turns.

Interface used by the daemon:

    mgr = CallManager(telephony, tts, stt, cfg)
    mgr.start(message, topic=None) -> TurnResult     # dial, ring, speak, listen
    mgr.say(call_id, message, expect_reply=True) -> TurnResult
    mgr.end(call_id, message=None) -> TurnResult     # IN_CALL if they talk over the goodbye
    mgr.active() -> dict | None                      # {"call_id", "state", "started_at", "turns"}
    mgr.close()

All methods block until their turn finishes and are safe to call from
different HTTP handler threads (one call at a time; a second start returns
Status.LINE_BUSY). Policy is NOT checked here; the daemon does that first.

One turn = speak + listen. TTS sentences are queued on the call as soon as
each is synthesized (a producer thread), while this thread keeps reading
incoming frames into a TurnDetector so the user can barge in. Listening (an
_Ear) never stops: in a turn and between turns alike, a short "yeah" can play
in a pause, a phrase that only asks "can you hear me?" is answered at once,
and a spoken ack plays while STT runs. Between turns a background thread also
plays thinking fillers and the cue sound, notices a remote hangup and enforces
idle_hangup_s / max_call_min. What the user says between turns is held and
returned instead of saying the agent's next message.
"""

import logging
import random
import threading
import time
import uuid
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paths
from .audio import FRAME_MS, SAMPLE_RATE
from .audio.vad import TurnEvent
from .config import Config
from .fillers import Fillers, asks_if_heard, ends_asking_if_heard, hesitation, is_ours
from .status import Status, TurnResult
from .stt.base import STT
from .telephony.base import Call, CallState, EndReason, Telephony
from .tts.base import TTS

log = logging.getLogger(__name__)

FRAME_S = FRAME_MS / 1000
GOODBYE_IDLE = "I have to go now. I'll write the rest in the terminal."
CUE_INTERVAL_S = 4.0         # between-turns sounds: a spoken filler, then the cue sound, and so on
FIRST_CUE_S = 1.5            # most agent replies take 3-6 s; a quicker one needs no filler
BARGE_GRACE_S = 0.4          # ignore the first moment of our own playback (line echo); later turns
READ_POLL_S = 0.05
# A "yeah" in a pause while the user talks. Their turn ends after end_silence_ms of silence,
# so a shorter pause is a breath, a good moment to show we're listening.
BACKCHANNEL_PAUSE_MS = 400
BACKCHANNEL_FIRST_S = 4.0    # talking this long earns the first one; short replies get none
BACKCHANNEL_GAP_S = (5.0, 8.0)   # then a random wait in this range between them
BACKCHANNEL_MIN_PHRASE_S = 1.5  # a short phrase may be a question ("Can you hear me?")
# A pause this long ends a phrase. Its STT runs in the background: a phrase that only asks
# "can you hear me?" gets an answer at once, and a one-phrase turn has its text sooner.
PHRASE_PAUSE_FRAMES = 300 // FRAME_MS
NOT_SAID = ("your message was not said: they spoke while you were working. Answer what they "
            "said; repeat what still matters from your message.")
NOT_HUNG_UP = "not hung up: they spoke while you were working. Answer what they said."

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
    wav: wave.Wave_write | None = None
    ear: "_Ear | None" = None           # the between-turns listener, handed to the next turn
    pending: list[str] = field(default_factory=list)   # said while the agent worked

    def note(self, who: str, text: str) -> None:
        if text:
            self.lines.append((datetime.now(), who, text))


def _db(det) -> str:
    v = getattr(det, "noise_floor_db", None)
    return "n/a" if v is None else f"{v:.1f} dBFS"


class _CallEnded(Exception):
    pass


class _Peek:
    """STT of one phrase, run in the background during the pause after it."""

    def __init__(self, stt: STT, pcm: bytes, at: int):
        self.at = at                    # frame count when it began
        self.ok = True
        self.text = ""
        self._seen = False
        self._thread = threading.Thread(target=self._run, args=(stt, pcm),
                                        name="callmaibe-peek", daemon=True)
        self._thread.start()

    def _run(self, stt: STT, pcm: bytes) -> None:
        try:
            self.text = stt.transcribe(pcm)
        except Exception:
            log.exception("STT in a pause failed")
            self.ok = False

    def fresh(self) -> bool:
        """True once, when the text is ready."""
        if self._seen or self._thread.is_alive():
            return False
        self._seen = True
        return True

    def result(self) -> str:
        self._thread.join()
        self._seen = True
        return self.text


class _Ear:
    """Hears the person frame by frame, the same way in a turn and while the agent works.

    A pause of PHRASE_PAUSE_FRAMES ends a phrase, and its STT runs in the background: a phrase
    that ends asking "can you hear me?" gets an answer at once. A longer pause after a longer
    phrase can get a "yeah". When the detector ends the utterance, an ack plays while the last
    phrase's STT finishes, and feed() returns the phrases' texts joined, or "" when there is
    nothing to pass on (noise, "uh", a check). So a long answer costs one phrase of STT at its
    end, not all of it again.
    """

    def __init__(self, mgr: "CallManager", sess: _Session, det, barged: bool = False,
                 label: str = "after listen start"):
        self._mgr = mgr
        self._sess = sess
        self.det = det
        self._label = label
        self._heard = 0                 # frames fed: the call's clock
        self._new_utterance(barged)

    @property
    def in_speech(self) -> bool:
        return bool(self.det.in_speech) or self._barged

    def _new_utterance(self, barged: bool) -> None:
        self._barged = barged           # it began while we spoke
        self._started_s = 0.0 if barged else None
        self._pause = 0                 # unvoiced frames in a row
        self._voiced_at = 0             # frame count at the latest voiced frame
        self._phrase_bytes = 0          # where the latest phrase starts in det.utterance()
        self._phrase_at = self._heard   # ...and at which frame
        self._phrase_s = 0.0            # length of the phrase that just ended
        self._peeks: list[_Peek] = []   # one per phrase, in order
        self._next_bc = self._heard * FRAME_S + BACKCHANNEL_FIRST_S
        self._holding = False           # our "yeah" or answer plays: their pause doesn't count yet
        self._spoke_last = False        # ...and they haven't talked past it
        self._answered = False          # ...and it was the answer to "can you hear me?"

    def feed(self, frame: bytes) -> str | None:
        mgr, call = self._mgr, self._sess.call
        c = mgr._cfg.conversation
        self._heard += 1
        now_s = self._heard * FRAME_S
        self._holding = self._holding and call.playing()
        if self._holding:
            self.det.hold()
        ev = self.det.feed(frame)
        if self.det.last_voiced:
            self._pause, self._voiced_at = 0, self._heard
            if not self._holding:
                self._spoke_last = self._answered = False
        else:
            self._pause += 1
        peek = self._peeks[-1] if self._peeks else None
        if peek is not None and peek.fresh() and c.fillers and ends_asking_if_heard(peek.text):
            mgr._answer_check(self._sess)
            self._holding = self._spoke_last = self._answered = True
        if ev is TurnEvent.SPEECH_START:
            self._started_s = now_s
            self._phrase_at = self._heard
            self._phrase_bytes, self._peeks = 0, []     # det.utterance() starts here
            self._next_bc = now_s + BACKCHANNEL_FIRST_S
        elif ev is TurnEvent.TURN_END:
            return self._utterance_end(now_s)
        elif ev is TurnEvent.NONE and self.in_speech:
            if self._pause == PHRASE_PAUSE_FRAMES:
                self._phrase_end()
            elif (c.fillers and self._pause * FRAME_MS >= BACKCHANNEL_PAUSE_MS
                  and now_s >= self._next_bc and self._phrase_s >= BACKCHANNEL_MIN_PHRASE_S
                  and not call.playing()):
                if bc := mgr._fillers.backchannel():
                    call.play(bc)
                    self._holding = self._spoke_last = True
                    log.info("call %s: backchannel %.1f s %s", self._sess.call_id, now_s,
                             self._label)
                self._next_bc = now_s + random.uniform(*BACKCHANNEL_GAP_S)
        return None

    def _phrase_end(self) -> None:
        pcm = self.det.utterance()
        self._peeks.append(_Peek(self._mgr._stt, pcm[self._phrase_bytes:], at=self._heard))
        self._phrase_bytes = len(pcm)
        self._phrase_s = (self._voiced_at - self._phrase_at) * FRAME_S
        self._phrase_at = self._heard

    def _utterance_end(self, now_s: float) -> str:
        mgr, sess, call = self._mgr, self._sess, self._sess.call
        c = mgr._cfg.conversation
        pcm = self.det.utterance()
        began = "?" if self._started_s is None else f"{self._started_s:.2f}"
        log.info("call %s: turn: speech start %s s, end %.2f s %s, utterance %.2f s, "
                 "noise floor %s, barge-in %s", sess.call_id, began, now_s, self._label,
                 len(pcm) / (SAMPLE_RATE * 2), _db(self.det), "yes" if self._barged else "no")
        t_stt = time.monotonic()
        peeks = self._peeks
        covered = bool(peeks) and self._voiced_at < peeks[-1].at
        if covered:                     # STT of the last phrase began in the pause: use it first
            last = peeks[-1].result()
            if c.fillers and not self._answered and ends_asking_if_heard(last):
                mgr._answer_check(sess)
                self._answered = self._spoke_last = True
        ack = None
        if c.fillers and not (self._spoke_last or call.playing()):
            ack = mgr._fillers.ack()            # "Got it." while STT runs
            if ack:                             # (a "yeah" they didn't talk past is enough)
                call.play(ack)
        texts = [p.result() for p in peeks]
        if all(p.ok for p in peeks):
            if not covered:     # speech after the last pause: max_turn_s cut them off, or no pause
                texts.append(mgr._stt.transcribe(pcm[self._phrase_bytes:]))
            text = " ".join(t for t in texts if t)
        else:
            text = mgr._stt.transcribe(pcm)
        log.info("call %s: timing: turn end -> STT done %.2f s (%d phrases begun in pauses)",
                 sess.call_id, time.monotonic() - t_stt, len(peeks))
        if c.fillers and not self._answered and ends_asking_if_heard(text):
            mgr._answer_check(sess)
        if c.fillers and asks_if_heard(text):           # only the check: nothing to pass on
            sess.note("You", text + " [the phone answered]")
            text = ""
        elif not text or hesitation(text):
            log.info("call %s: %s, nothing to pass on", sess.call_id,
                     "only a hesitation" if text else "empty transcription")
            if ack:
                call.stop_playback()
            text = ""
        self.det.reset()
        self._new_utterance(barged=False)
        return text


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
        fillers: Fillers | None = None,
    ):
        self._tel = telephony
        self._tts = tts
        self._stt = stt
        self._cfg = cfg
        self._detector_factory = detector_factory or self._default_detector
        self._fillers = fillers or Fillers(tts, cfg.tts)
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
                    return self._turn(sess, message, expect_reply=False, ending=True)
                if held := self._held(sess):
                    return self._between_turns(sess, TurnResult(
                        Status.IN_CALL, user_said=held, call_id=sess.call_id, reason=NOT_HUNG_UP))
                return self._finish(sess, TurnResult(Status.ENDED, call_id=sess.call_id))
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

    def warmup(self) -> None:
        """Prepare the spoken fillers: slow the first time, so call it from a background thread."""
        if self._cfg.conversation.fillers:
            self._fillers.render(busy=lambda: self._active is not None)

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
        if sess.wav is not None:
            try:
                sess.wav.close()
            except Exception:
                log.exception("call %s: could not close debug audio", sess.call_id)
            sess.wav = None
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
    def _turn(self, sess: _Session, message: str, expect_reply: bool,
              ending: bool = False) -> TurnResult:
        """Speak `message`, then listen. The person can talk over any message, a goodbye
        (`ending`) too; then the call stays up. After a goodbye in full we hang up at once.
        If they spoke while the agent worked, `message` is not said: they go first."""
        call = sess.call
        c = self._cfg.conversation
        sess.turns += 1
        try:
            if held := self._held(sess):
                sess.note("Agent", message + " [not said: they spoke first]")
                log.info("call %s: message held back, they spoke while the agent worked",
                         sess.call_id)
                return self._between_turns(sess, TurnResult(
                    Status.IN_CALL, user_said=held, call_id=sess.call_id, reason=NOT_SAID))
            sess.note("Agent", message)
            det = self._detector_factory()
            grace = c.first_turn_grace_s if sess.turns == 1 else BARGE_GRACE_S
            barge = c.barge_in and call.full_duplex
            barged = self._speak(sess, message, barge=barge, det=det, grace_s=grace)
            if barged:
                sess.lines[-1] = (*sess.lines[-1][:2], message + " [interrupted]")
            wait_s = c.listen_timeout_s if expect_reply else 0.0
            ear = _Ear(self, sess, det, barged=barged)
            text = self._listen(sess, ear, wait_s) if ear.in_speech or wait_s else None
        except _CallEnded as e:
            text = e.args[0] if e.args else ""
            sess.note("You", text)
            return self._finish(sess, TurnResult(Status.ENDED_BY_USER, user_said=text,
                                                 call_id=sess.call_id))
        if text is not None:
            sess.note("You", text)
            return self._between_turns(sess, TurnResult(Status.IN_CALL, user_said=text,
                                                        call_id=sess.call_id))
        if ending:
            return self._finish(sess, TurnResult(Status.ENDED, call_id=sess.call_id))
        status = Status.NO_REPLY if expect_reply else Status.IN_CALL
        return self._between_turns(sess, TurnResult(status, call_id=sess.call_id))

    def _speak(self, sess: _Session, text: str, barge: bool, det=None,
               grace_s: float = BARGE_GRACE_S) -> bool:
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

        t_speak = time.monotonic()

        def produce():
            first = True
            try:
                for chunk in self._tts.synthesize(text):
                    with plock:
                        if cancel.is_set():
                            break
                        call.play(chunk)
                    if first:
                        first = False
                        log.info("call %s: timing: message -> first TTS chunk queued %.2f s",
                                 sess.call_id, time.monotonic() - t_speak)
            except BaseException as e:  # noqa: BLE001 - re-raised in the turn thread
                errors.append(e)
            finally:
                done.set()

        sess.producer = threading.Thread(target=produce, name="callmaibe-tts", daemon=True)
        sess.producer.start()
        played_frames = 0
        armed = False
        need = max(1, self._cfg.conversation.barge_in_min_ms // FRAME_MS)
        recent: deque[bool] = deque(maxlen=need + 300 // FRAME_MS)   # voiced flags, sliding window
        try:
            while True:
                if errors:
                    raise errors[0]
                frame = self._read(sess, READ_POLL_S)
                if frame is None:
                    if call.state is CallState.ENDED:
                        raise _CallEnded(self._partial(det) if armed else "")
                elif barge and not armed:
                    if call.playing():
                        played_frames += 1
                        armed = played_frames * FRAME_S >= grace_s
                elif barge:
                    ev = det.feed(frame)
                    recent.append(bool(det.last_voiced))
                    if sum(recent) >= need:
                        with plock:
                            cancel.set()
                            call.stop_playback()
                        log.info("call %s: barge-in after %d voiced frames, noise floor %s",
                                 sess.call_id, sum(recent), _db(det))
                        return True
                    if ev is TurnEvent.TURN_END:       # a short noise, not an interruption
                        det.reset()
                        recent.clear()
                if done.is_set() and not call.playing():
                    if errors:
                        raise errors[0]
                    return False
        finally:
            cancel.set()
            if not done.is_set():       # barge-in or hangup: don't finish the sentence being made
                self._tts.stop()

    def _listen(self, sess: _Session, ear: _Ear, wait_s: float) -> str | None:
        """Listen until they finish saying something to pass on; None if nobody started
        within wait_s."""
        call = sess.call
        c = self._cfg.conversation
        t0, frames = time.monotonic(), 0
        while True:
            frame = self._read(sess, READ_POLL_S * 2)
            if frame is None:
                if call.state is CallState.ENDED:
                    raise _CallEnded(self._partial(ear.det) if ear.in_speech else "")
            else:
                frames += 1
                text = ear.feed(frame)
                if text:
                    return text
                if text == "":                      # noise, "uh" or a line check: listen on
                    t0, frames = time.monotonic(), 0
            # frames arrive in real time on a real line; the frame count also keeps fast fakes honest
            elapsed = max(time.monotonic() - t0, frames * FRAME_S)
            if not ear.in_speech and elapsed >= wait_s:
                return None
            if ear.in_speech and elapsed >= wait_s + c.max_turn_s + 5:
                log.warning("call %s: detector never ended the turn; forcing it", sess.call_id)
                return self._stt.transcribe(ear.det.utterance())

    def _held(self, sess: _Session) -> str:
        """What they said while the agent worked, including what they are saying right now."""
        ear, sess.ear = sess.ear, None
        if ear is not None and ear.in_speech and (text := self._listen(sess, ear, 0.0)):
            sess.pending.append(text)
            sess.note("You", text)
        said, sess.pending = " ".join(sess.pending), []
        return said

    def _answer_check(self, sess: _Session) -> None:
        """Answer "can you hear me?" right away, instead of waiting for the agent."""
        sess.call.stop_playback()
        if reply := self._fillers.heard_you():
            sess.call.play(reply)
        log.info("call %s: answered a can-you-hear-me", sess.call_id)

    def _read(self, sess: _Session, timeout: float) -> bytes | None:
        """call.read(), plus the debug recording of incoming audio when enabled."""
        frame = sess.call.read(timeout)
        if frame is not None and self._cfg.conversation.debug_audio:
            try:
                if sess.wav is None:
                    d = paths.logs_dir() / "audio"
                    d.mkdir(exist_ok=True)
                    sess.wav = wave.open(str(d / f"{sess.call_id}-in.wav"), "wb")  # noqa: SIM115
                    sess.wav.setnchannels(1)
                    sess.wav.setsampwidth(2)
                    sess.wav.setframerate(SAMPLE_RATE)
                sess.wav.writeframes(frame)
            except Exception:
                log.exception("call %s: debug audio failed; disabling", sess.call_id)
                self._cfg.conversation.debug_audio = False
        return frame

    def _partial(self, det) -> str:
        if det is None:
            return ""
        pcm = det.utterance()
        return self._stt.transcribe(pcm) if pcm else ""

    # ------------------------------------------------------------------ between turns
    def _between_turns(self, sess: _Session, result: TurnResult) -> TurnResult:
        sess.idle_stop = threading.Event()
        sess.idle_thread = threading.Thread(target=self._idle,
                                            args=(sess, sess.idle_stop, bool(result.user_said)),
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

    def _idle_sound(self, slot: int, heard: bool) -> bytes | None:
        """Even slots speak a filler: first "Let me check." (only after the user spoke), then
        "Still on it." ones. Other slots, and any without a filler ready, play the cue sound."""
        if slot % 2 == 0 and self._cfg.conversation.fillers and (heard or slot > 0):
            pcm = self._fillers.thinking(first=slot == 0 and heard)
            if pcm:
                return pcm
        return self._cue()

    def _idle(self, sess: _Session, stop: threading.Event, heard: bool = False) -> None:
        call = sess.call
        c = self._cfg.conversation
        start = time.monotonic()
        idle_deadline = start + c.idle_hangup_s
        max_deadline = sess.answered_mono + c.max_call_min * 60
        next_cue = start + FIRST_CUE_S
        slot = 0
        ear = sess.ear = _Ear(self, sess, self._detector_factory(), label="between turns")
        try:
            while not stop.is_set():
                frame = self._read(sess, READ_POLL_S * 2)
                # what they say now goes to the agent before its next message (echo of our
                # own fillers aside)
                if frame is not None and (text := ear.feed(frame)) and not is_ours(text):
                    sess.pending.append(text)
                    sess.note("You", text)
                    log.info("call %s: heard while the agent works; it gets this first",
                             sess.call_id)
                if call.state is CallState.ENDED:
                    log.info("call %s: remote hangup between turns", sess.call_id)
                    self._remember(sess, TurnResult(Status.ENDED_BY_USER, call_id=sess.call_id,
                                                    user_said=" ".join(sess.pending),
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
                if now >= next_cue and not call.playing() and not ear.in_speech:
                    if pcm := self._idle_sound(slot, heard):    # (after any ack, not over them)
                        call.play(pcm)
                    slot += 1
                    next_cue = now + self._cue_interval
            # A filler still playing finishes: cut mid-word it sounds broken, and the agent's
            # first sentence takes about as long to synthesize anyway.
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
        return TurnDetector(end_silence_ms=c.end_silence_ms, max_turn_s=c.max_turn_s,
                            aggressiveness=c.vad_aggressiveness)
