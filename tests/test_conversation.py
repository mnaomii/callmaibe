"""Conversation engine against the fake telephony backend, fake TTS/STT and a scripted detector."""

import threading
import time
import wave
from pathlib import Path

import pytest

from callmaibe import conversation
from callmaibe.audio import FRAME_BYTES, SAMPLE_RATE
from callmaibe.audio.vad import TurnEvent
from callmaibe.config import Config
from callmaibe.conversation import GOODBYE_IDLE, CallManager
from callmaibe.status import Status
from callmaibe.stt.base import STT
from callmaibe.telephony.base import CallState
from callmaibe.telephony.fake import FakeTelephony
from callmaibe.tts.base import TTS

MS_PER_CHAR = 20


def speech(seconds: float) -> bytes:
    """Non-zero PCM: the fake detector treats any non-zero frame as speech."""
    return b"\x10\x00" * int(seconds * SAMPLE_RATE)


def tts_bytes(text: str) -> int:
    return len(text) * MS_PER_CHAR * SAMPLE_RATE // 1000 * 2


class FakeTTS(TTS):
    """Silence proportional to text length, one chunk per sentence."""

    def synthesize(self, text):
        for sentence in text.replace(". ", ".|").split("|"):
            if sentence:
                yield bytes(tts_bytes(sentence))


class FakeSTT(STT):
    def __init__(self, *replies):
        self.replies = list(replies)
        self.heard: list[int] = []

    def transcribe(self, pcm):
        self.heard.append(len(pcm))
        return self.replies.pop(0) if self.replies else ""


class FakeDetector:
    """Any non-zero frame is speech; 10 silent frames (200 ms) end the turn."""

    def __init__(self, end_frames: int = 10):
        self.end_frames = end_frames
        self.reset()

    def reset(self):
        self.buf = bytearray()
        self.in_speech = False
        self.silent = 0
        self.last_voiced = False

    def feed(self, frame):
        assert len(frame) == FRAME_BYTES
        loud = self.last_voiced = any(frame)
        if loud:
            self.silent = 0
            self.buf += frame
            if not self.in_speech:
                self.in_speech = True
                return TurnEvent.SPEECH_START
        elif self.in_speech:
            self.buf += frame
            self.silent += 1
            if self.silent >= self.end_frames:
                self.in_speech = False
                return TurnEvent.TURN_END
        return TurnEvent.NONE

    def utterance(self):
        return bytes(self.buf)

    def hold(self):
        self.silent = 0


def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLMAIBE_HOME", str(tmp_path))
    c = Config()
    c.callee.uri = "sip:me@example.org"
    c.conversation.ring_timeout_s = 5
    c.conversation.listen_timeout_s = 2
    c.conversation.idle_hangup_s = 30
    c.conversation.thinking_cue = False
    return c


@pytest.fixture
def make(cfg):
    managers = []

    def _make(*replies, cue=None, **script):
        tel = FakeTelephony(speed=100)
        if script:
            tel.script(**script)
        stt = FakeSTT(*replies)
        mgr = CallManager(tel, FakeTTS(), stt, cfg, detector_factory=FakeDetector, cue=cue)
        managers.append(mgr)
        return mgr, tel, stt

    yield _make
    for m in managers:
        m.close()


SHORT = "Hi. Task done."          # 0.28 s of audio
LONG = "This is a long update. " * 7   # about 3.2 s of audio


def test_answer_then_reply(make, cfg):
    mgr, tel, stt = make("sounds good", answer_after_s=1, incoming=[0.6, speech(0.5), 1.0])
    r = mgr.start(SHORT, topic="refactor")
    assert r.status is Status.IN_CALL
    assert r.user_said == "sounds good"
    assert len(r.call_id) == 8
    call = tel.last_call
    assert len(call.played) == tts_bytes("Hi.") + tts_bytes("Task done.")
    assert stt.heard and stt.heard[0] >= len(speech(0.5))
    assert mgr.active()["call_id"] == r.call_id
    assert mgr.active()["turns"] == 1


def test_barge_in_stops_playback(make, cfg):
    cfg.conversation.first_turn_grace_s = 0.4
    mgr, tel, stt = make("wait stop", incoming=[1.0, speech(0.9), 2.0])
    r = mgr.start(LONG)
    assert r.status is Status.IN_CALL and r.user_said == "wait stop"
    call = tel.last_call
    assert call.stop_count >= 1
    assert len(call.played) < call.queued_bytes
    assert len(call.played) < tts_bytes(LONG) * 3 // 4
    assert stt.heard[0] >= len(speech(0.9))        # the interrupting speech is kept for STT


class StuckTTS(FakeTTS):
    """Queues the whole message, then hangs as if making one more sentence, until stop()."""

    def __init__(self):
        self.stopped = threading.Event()

    def synthesize(self, text):
        yield bytes(tts_bytes(text))
        self.stopped.wait(5)

    def stop(self):
        self.stopped.set()


def test_barge_in_stops_speech_synthesis(cfg):
    cfg.conversation.first_turn_grace_s = 0.4
    tel = FakeTelephony(speed=100)
    tel.script(incoming=[1.0, speech(0.9), 2.0])
    tts = StuckTTS()
    mgr = CallManager(tel, tts, FakeSTT("wait"), cfg, detector_factory=FakeDetector)
    try:
        assert mgr.start(LONG).status is Status.IN_CALL
        assert tts.stopped.is_set()
    finally:
        mgr.close()


def test_no_barge_in_from_hello_during_first_turn_grace(make):
    mgr, tel, stt = make("second try", incoming=[0.5, speech(0.4), 3.2, speech(0.5), 2.0])
    r = mgr.start(LONG)
    assert r.status is Status.IN_CALL and r.user_said == "second try"
    call = tel.last_call
    assert call.stop_count == 0
    assert len(call.played) == call.queued_bytes
    assert len(stt.heard) == 1


def test_no_barge_in_from_short_noise_bursts(make, cfg):
    cfg.conversation.first_turn_grace_s = 0.4
    burst = speech(0.2)
    mgr, tel, _ = make("later", incoming=[1.0, burst, 0.5, burst, 0.5, burst, 1.0, speech(0.5), 2.0])
    r = mgr.start(LONG)
    assert r.status is Status.IN_CALL and r.user_said == "later"
    assert tel.last_call.stop_count == 0
    assert len(tel.last_call.played) == tel.last_call.queued_bytes


def test_barge_in_disabled(make, cfg):
    cfg.conversation.barge_in = False
    cfg.conversation.first_turn_grace_s = 0.4
    mgr, tel, stt = make("second try", incoming=[1.0, speech(1.2), 2.4, speech(0.5), 2.0])
    r = mgr.start(LONG)
    assert r.status is Status.IN_CALL and r.user_said == "second try"
    assert tel.last_call.stop_count == 0
    assert len(tel.last_call.played) == tel.last_call.queued_bytes
    assert len(stt.heard) == 1


def test_debug_audio_writes_wav(make, cfg, tmp_path):
    cfg.conversation.debug_audio = True
    mgr, _, _ = make("hi", incoming=[0.5, speech(0.4)])
    r = mgr.start(SHORT)
    assert mgr.end(r.call_id).status is Status.ENDED
    path = tmp_path / "logs" / "audio" / f"{r.call_id}-in.wav"
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, SAMPLE_RATE)
        assert w.getnframes() >= SAMPLE_RATE // 2


def test_no_debug_audio_by_default(make, tmp_path):
    mgr, _, _ = make("hi", incoming=[0.5, speech(0.4)])
    r = mgr.start(SHORT)
    mgr.end(r.call_id)
    assert not (tmp_path / "logs" / "audio").exists()


def test_barge_in_skipped_without_full_duplex(make):
    mgr, tel, stt = make("second try", full_duplex=False,
                         incoming=[1.0, speech(0.6), 2.4, speech(0.5), 2.0])
    r = mgr.start(LONG)
    assert r.status is Status.IN_CALL and r.user_said == "second try"
    call = tel.last_call
    assert call.stop_count == 0
    assert len(call.played) == call.queued_bytes > 3 * SAMPLE_RATE * 2
    assert len(stt.heard) == 1


def test_no_reply(make):
    mgr, tel, stt = make()
    r = mgr.start(SHORT)
    assert r.status is Status.NO_REPLY
    assert tel.last_call.state is CallState.ACTIVE
    assert mgr.active() is not None
    assert stt.heard == []


def test_remote_hangup_mid_listen_returns_partial(make):
    mgr, _, _ = make("half a sentence", incoming=[0.4, speech(1.0)], hangup_at_s=1.0)
    r = mgr.start(SHORT)
    assert r.status is Status.ENDED_BY_USER
    assert r.user_said == "half a sentence"
    assert mgr.active() is None
    assert r.transcript_path


@pytest.mark.parametrize("script,status", [
    ({"answer": "decline", "answer_after_s": 1}, Status.DECLINED),
    ({"answer": "busy"}, Status.BUSY),
    ({"answer": "never"}, Status.NO_ANSWER),
    ({"answer": "answer", "answer_after_s": 60}, Status.NO_ANSWER),
    ({"answer": "error", "error_detail": "488 Not Acceptable Here"}, Status.ERROR),
])
def test_not_answered(make, script, status):
    mgr, tel, _ = make(**script)
    r = mgr.start(SHORT)
    assert r.status is status
    assert mgr.active() is None
    assert tel.last_call.played == b""
    if status is Status.ERROR:
        assert "488" in r.reason


def test_line_busy(make):
    mgr, _, _ = make("yes", incoming=[0.5, speech(0.3)])
    first = mgr.start(SHORT)
    assert first.status is Status.IN_CALL
    assert mgr.start("another").status is Status.LINE_BUSY
    assert mgr.end(first.call_id).status is Status.ENDED
    assert mgr.active() is None


def test_not_found(make):
    mgr, _, _ = make("yes", incoming=[0.5, speech(0.3)])
    assert mgr.say("deadbeef", "hello").status is Status.NOT_FOUND
    r = mgr.start(SHORT)
    assert mgr.say("deadbeef", "hello").status is Status.NOT_FOUND
    assert mgr.end("deadbeef").status is Status.NOT_FOUND
    assert mgr.end(r.call_id).status is Status.ENDED
    assert mgr.say(r.call_id, "hello").status is Status.NOT_FOUND


def test_idle_hangup(make, cfg):
    cfg.conversation.idle_hangup_s = 0.3
    mgr, tel, _ = make("ok", incoming=[0.5, speech(0.3)])
    r = mgr.start(SHORT)
    assert r.status is Status.IN_CALL
    call = tel.last_call
    assert wait_for(lambda: call.state is CallState.ENDED)
    assert wait_for(lambda: mgr.active() is None)
    r2 = mgr.say(r.call_id, "are you there?")
    assert r2.status is Status.ENDED
    assert "idle" in r2.reason
    assert GOODBYE_IDLE in Path(r2.transcript_path).read_text(encoding="utf-8")
    assert len(call.played) >= tts_bytes(SHORT) + tts_bytes(GOODBYE_IDLE) - 4 * FRAME_BYTES
    assert mgr.say(r.call_id, "again").status is Status.NOT_FOUND


def test_remote_hangup_between_turns(make):
    mgr, tel, _ = make("ok", incoming=[0.5, speech(0.3)], hangup_at_s=3.0)
    r = mgr.start(SHORT)
    assert r.status is Status.IN_CALL
    assert wait_for(lambda: tel.last_call.state is CallState.ENDED)
    r2 = mgr.say(r.call_id, "next question")
    assert r2.status is Status.ENDED_BY_USER
    assert mgr.active() is None


def test_say_without_reply(make):
    mgr, tel, _ = make("go ahead", incoming=[0.5, speech(0.3)])
    r = mgr.start(SHORT)
    before = len(tel.last_call.played)
    r2 = mgr.say(r.call_id, "Okay, starting now.", expect_reply=False)
    assert r2.status is Status.IN_CALL and r2.user_said == ""
    assert len(tel.last_call.played) - before >= tts_bytes("Okay, starting now.")
    assert not tel.last_call.playing()


def test_end_writes_transcript(make):
    mgr, tel, _ = make("sounds good", incoming=[0.5, speech(0.4)])
    r = mgr.start(SHORT, topic="db migration")
    r2 = mgr.end(r.call_id, "Bye now.")
    assert r2.status is Status.ENDED
    assert tel.last_call.state is CallState.ENDED
    text = Path(r2.transcript_path).read_text(encoding="utf-8")
    assert "Topic: db migration" in text
    assert "Agent: Hi. Task done." in text
    assert "You: sounds good" in text
    assert "Agent: Bye now." in text
    assert r.call_id in r2.transcript_path


@pytest.mark.parametrize("final", ["end", "say"])
def test_talking_over_a_statement_keeps_the_call_up(make, final):
    mgr, tel, _ = make("ok", "one more thing", speed=20,
                       incoming=[0.5, speech(0.3), 0.8, speech(2.0), 1.0])
    r = mgr.start(SHORT)
    if final == "end":
        r2 = mgr.end(r.call_id, LONG)
    else:
        r2 = mgr.say(r.call_id, LONG, expect_reply=False)
    assert r2.status is Status.IN_CALL and r2.user_said == "one more thing"
    call = tel.last_call
    assert call.state is CallState.ACTIVE and mgr.active() is not None
    assert len(call.played) < call.queued_bytes        # the statement was cut short
    r3 = mgr.end(r.call_id, "Got it. Bye.")
    assert r3.status is Status.ENDED
    text = Path(r3.transcript_path).read_text(encoding="utf-8")
    assert "[interrupted]" in text and "You: one more thing" in text


def test_end_hangs_up_right_after_the_goodbye(make):
    mgr, tel, stt = make("ok", "too late", speed=20,
                         incoming=[0.5, speech(0.3), 1.5, speech(0.6), 1.0])
    r = mgr.start(SHORT)
    assert mgr.end(r.call_id, "Bye now.").status is Status.ENDED
    call = tel.last_call
    assert call.state is CallState.ENDED and call.stop_count == 0   # the goodbye played in full
    assert call.virtual_s < 2.3                    # hung up before they spoke again at 2.3 s
    assert len(stt.heard) == 1


def test_thinking_cue_between_turns(make, cfg, monkeypatch):
    monkeypatch.setattr(conversation, "FIRST_CUE_S", 0.0)
    cfg.conversation.thinking_cue = True
    cue = b"\x07\x00" * 320
    mgr, tel, _ = make("ok", cue=lambda: cue, incoming=[0.5, speech(0.3)])
    r = mgr.start(SHORT)
    assert r.status is Status.IN_CALL
    assert wait_for(lambda: cue in bytes(tel.last_call.played))
    assert mgr.end(r.call_id).status is Status.ENDED


class FakeFillers:
    BC = b"\x04\x00" * 12800           # 0.8 s, like a real "yeah"
    ACK = b"\x05\x00" * 1600
    THINK = b"\x06\x00" * 1600

    def __init__(self):
        self.asked = []

    def backchannel(self):
        self.asked.append("bc")
        return self.BC

    def ack(self):
        self.asked.append("ack")
        return self.ACK

    def thinking(self, first):
        self.asked.append("first" if first else "still")
        return self.THINK


def test_fillers_ack_then_think_while_the_agent_works(cfg, monkeypatch):
    monkeypatch.setattr(conversation, "FIRST_CUE_S", 0.0)
    cfg.conversation.thinking_cue = True
    cue = b"\x07\x00" * 320
    tel = FakeTelephony(speed=100)
    tel.script(incoming=[0.5, speech(0.3)])
    f = FakeFillers()
    mgr = CallManager(tel, FakeTTS(), FakeSTT("ok"), cfg, detector_factory=FakeDetector,
                      cue=lambda: cue, cue_interval_s=0.05, fillers=f)
    try:
        r = mgr.start(SHORT)
        assert r.status is Status.IN_CALL
        # ack while STT runs; then filler, cue sound, "still" filler while the agent works
        assert wait_for(lambda: f.asked[:3] == ["ack", "first", "still"]), f.asked
        assert wait_for(lambda: all(x in bytes(tel.last_call.played) for x in (f.ACK, f.THINK, cue)))
        # after a statement nobody answered, there is no "let me check" to say
        assert mgr.say(r.call_id, "Running the tests now.", expect_reply=False).status is Status.IN_CALL
        n = len(f.asked)
        assert wait_for(lambda: len(f.asked) > n + 1)
        assert f.asked.count("first") == 1
    finally:
        mgr.close()


@pytest.mark.parametrize("go_on", [True, False])
def test_backchannel_gives_time_to_go_on(cfg, go_on):
    # 1 s of silence ends the turn. They pause 1.2 s after 4.5 s of talking: a "yeah" plays
    # 0.4 s in, and the silence count restarts after it, so going on keeps it one turn.
    rest = [speech(1.0), 1.5] if go_on else [2.0]
    tel = FakeTelephony(speed=100)
    tel.script(incoming=[0.5, speech(4.5), 1.2, *rest])
    f, stt = FakeFillers(), FakeSTT("long story")
    mgr = CallManager(tel, FakeTTS(), stt, cfg,
                      detector_factory=lambda: FakeDetector(end_frames=50), fillers=f)
    try:
        r = mgr.start(SHORT)
        assert r.status is Status.IN_CALL and r.user_said == "long story"
        assert f.BC in bytes(tel.last_call.played)
        if go_on:
            assert stt.heard[0] >= len(speech(7.5))     # 4.5 + 1.2 pause + 1.0 + 1.0: one turn
            assert f.asked == ["bc", "ack"]
        else:
            assert f.asked == ["bc"]                    # a "yeah" they didn't talk past: no ack
    finally:
        mgr.close()
