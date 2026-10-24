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
incoming frames into a TurnDetector so the user can barge in. When the user
pauses mid-turn, a short "yeah" can play; when they stop, a spoken ack plays
while STT runs. Between turns a
background thread plays thinking fillers and the cue sound, drains incoming
audio, notices a remote hangup and enforces idle_hangup_s / max_call_min.
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
from .fillers import Fillers
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

    def note(self, who: str, text: str) -> None:
        if text:
            self.lines.append((datetime.now(), who, text))


def _db(det) -> str:
    v = getattr(det, "noise_floor_db", None)
    return "n/a" if v is None else f"{v:.1f} dBFS"


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
        (`ending`) too; then the call stays up. After a goodbye in full we hang up at once."""
        call = sess.call
        c = self._cfg.conversation
        sess.turns += 1
        sess.note("Agent", message)
        det = self._detector_factory()
        try:
            grace = c.first_turn_grace_s if sess.turns == 1 else BARGE_GRACE_S
            barge = c.barge_in and call.full_duplex
            barged = self._speak(sess, message, barge=barge, det=det, grace_s=grace)
            if barged:
                sess.lines[-1] = (*sess.lines[-1][:2], message + " [interrupted]")
            wait_s = c.listen_timeout_s if expect_reply else 0.0
            talking = barged or det.in_speech
            text = self._listen(sess, det, talking, wait_s) if talking or wait_s else None
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

    def _listen(self, sess: _Session, det, speech_started: bool, wait_s: float) -> str | None:
        """Listen until TURN_END; return the transcript, or None if nobody started within wait_s."""
        call = sess.call
        c = self._cfg.conversation
        barged = speech_started
        t0, frames = time.monotonic(), 0
        listen_start = t0
        started_at = 0.0 if speech_started else None     # relative to listen start
        heard = pause = 0               # frames read (the call's clock), unvoiced frames in a row
        next_bc = BACKCHANNEL_FIRST_S   # in seconds of that clock
        holding = bc_unanswered = False
        while True:
            frame = self._read(sess, READ_POLL_S * 2)
            if frame is None:
                if call.state is CallState.ENDED:
                    raise _CallEnded(self._partial(det) if speech_started else "")
            else:
                frames += 1
                heard += 1
                now_s = heard * FRAME_S
                holding = holding and call.playing()
                if holding:
                    det.hold()          # our "yeah" is playing: their pause doesn't count yet
                ev = det.feed(frame)
                pause = 0 if det.last_voiced else pause + 1
                if det.last_voiced and not holding:
                    bc_unanswered = False
                if ev is TurnEvent.SPEECH_START:
                    speech_started = True
                    started_at = time.monotonic() - listen_start
                    next_bc = now_s + BACKCHANNEL_FIRST_S
                elif (ev is TurnEvent.NONE and speech_started and c.fillers and now_s >= next_bc
                      and pause * FRAME_MS >= BACKCHANNEL_PAUSE_MS and not call.playing()):
                    if bc := self._fillers.backchannel():          # "yeah" while they talk
                        call.play(bc)
                        holding = bc_unanswered = True
                        log.info("call %s: backchannel %.1f s into the turn", sess.call_id, now_s)
                    next_bc = now_s + random.uniform(*BACKCHANNEL_GAP_S)
                elif ev is TurnEvent.TURN_END:
                    pcm = det.utterance()
                    began = "?" if started_at is None else f"{started_at:.2f}"
                    log.info("call %s: turn: speech start %s s, end %.2f s after listen start, "
                             "utterance %.2f s, noise floor %s, barge-in %s",
                             sess.call_id, began,
                             time.monotonic() - listen_start, len(pcm) / (SAMPLE_RATE * 2),
                             _db(det), "yes" if barged else "no")
                    # a "yeah" they didn't talk past is ack enough
                    ack = (self._fillers.ack() if c.fillers and not call.playing()
                           and not bc_unanswered else None)
                    if ack:                                  # "Got it." while STT runs
                        call.play(ack)
                    t_stt = time.monotonic()
                    text = self._stt.transcribe(pcm)
                    log.info("call %s: timing: turn end -> STT done %.2f s",
                             sess.call_id, time.monotonic() - t_stt)
                    if text:
                        return text
                    log.info("call %s: empty transcription, still listening", sess.call_id)
                    if ack:
                        call.stop_playback()
                    det.reset()
                    speech_started = False
                    t0, frames = time.monotonic(), 0
                    continue
            # frames arrive in real time on a real line; the frame count also keeps fast fakes honest
            elapsed = max(time.monotonic() - t0, frames * FRAME_S)
            if not speech_started and elapsed >= wait_s:
                return None
            if speech_started and elapsed >= wait_s + c.max_turn_s + 5:
                log.warning("call %s: detector never ended the turn; forcing it", sess.call_id)
                return self._stt.transcribe(det.utterance())

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
        try:
            while not stop.is_set():
                self._read(sess, READ_POLL_S * 2)    # drain and discard
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
                if now >= next_cue and not call.playing():     # wait for the ack to finish
                    if pcm := self._idle_sound(slot, heard):
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
