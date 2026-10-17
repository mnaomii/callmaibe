import time

import numpy as np
import pytest

from callmaibe.audio import FRAME_BYTES, SAMPLE_RATE, cues
from callmaibe.audio.resample import from_internal, to_internal
from callmaibe.audio.vad import TurnDetector, TurnEvent
from callmaibe.config import STTCfg, TTSCfg
from callmaibe.tts.kokoro import split_sentences

VOICED = b"\x01" * FRAME_BYTES
SILENT = b"\x00" * FRAME_BYTES


def make_detector(monkeypatch, **kw) -> TurnDetector:
    det = TurnDetector(**kw)
    monkeypatch.setattr(det, "_voiced", lambda frame: frame[0] == 1)
    return det


def feed_all(det, frames):
    return [det.feed(f) for f in frames]


def test_no_speech_no_events(monkeypatch):
    det = make_detector(monkeypatch)
    assert set(feed_all(det, [SILENT] * 100)) == {TurnEvent.NONE}
    assert not det.in_speech


def test_start_once_then_end_on_silence(monkeypatch):
    det = make_detector(monkeypatch, end_silence_ms=400)
    evs = feed_all(det, [VOICED] * 30 + [SILENT] * 30)
    assert evs.count(TurnEvent.SPEECH_START) == 1
    assert evs.count(TurnEvent.TURN_END) == 1
    assert evs.index(TurnEvent.SPEECH_START) < evs.index(TurnEvent.TURN_END)
    assert not det.in_speech


def test_in_speech_between_start_and_end(monkeypatch):
    det = make_detector(monkeypatch)
    feed_all(det, [VOICED] * 20)
    assert det.in_speech


def test_max_turn(monkeypatch):
    det = make_detector(monkeypatch, end_silence_ms=1200, max_turn_s=1)
    evs = feed_all(det, [VOICED] * 100)
    assert TurnEvent.TURN_END in evs
    assert evs.index(TurnEvent.TURN_END) < 70


def test_preroll_and_trim(monkeypatch):
    det = make_detector(monkeypatch, end_silence_ms=1000)
    lead = [b"\x00" + b"\x07" * (FRAME_BYTES - 1)] * 50  # unvoiced, distinguishable
    feed_all(det, lead + [VOICED] * 30 + [SILENT] * 60)
    n = len(det.utterance()) // FRAME_BYTES
    # <= 15 pre-roll frames + 30 speech + <= 10 trailing silence
    assert 30 + 10 <= n <= 15 + 30 + 10 + 1
    assert det.utterance().endswith(SILENT * 10)
    assert not det.utterance().endswith(SILENT * 11)


def test_reset(monkeypatch):
    det = make_detector(monkeypatch, end_silence_ms=200)
    feed_all(det, [VOICED] * 20 + [SILENT] * 20)
    det.reset()
    assert det.utterance() == b""
    evs = feed_all(det, [VOICED] * 20 + [SILENT] * 20)
    assert TurnEvent.SPEECH_START in evs and TurnEvent.TURN_END in evs


def test_resample_roundtrip():
    x = (0.5 * np.sin(2 * np.pi * 440 * np.arange(24000) / 24000)).astype(np.float32)
    pcm = to_internal(x, 24000)
    assert isinstance(pcm, bytes) and len(pcm) == 16000 * 2
    y = from_internal(pcm, 24000)
    assert y.dtype == np.float32 and abs(len(y) - 24000) <= 2
    assert from_internal(pcm).shape == (16000,)


def test_to_internal_int16():
    x = np.full(1600, 1000, dtype=np.int16)
    assert to_internal(x, 16000) == x.tobytes()


@pytest.mark.parametrize("cue", [cues.thinking, cues.beep])
def test_cues(cue):
    pcm = cue()
    assert pcm and len(pcm) % 2 == 0
    s = np.frombuffer(pcm, dtype="<i2")
    assert 0 < np.abs(s).max() < 32767 * 0.5


def test_split_sentences():
    out = split_sentences("**Done.** See https://x.com/a now! `ok`?\n- next   item")
    assert out[0] == "Done."
    assert "link" in out[1] and "http" not in out[1]
    long = ", ".join(["word " * 8] * 10)
    assert all(len(s) < 260 for s in split_sentences(long)) and len(split_sentences(long)) > 1


def test_factories_reject_unknown():
    from callmaibe import stt, tts

    with pytest.raises(ValueError):
        tts.create(TTSCfg(engine="nope"))
    with pytest.raises(ValueError):
        stt.create(STTCfg(engine="nope"))


@pytest.fixture(scope="module")
def spoken(tmp_path_factory):
    import os

    from callmaibe import tts

    home = tmp_path_factory.mktemp("home")
    os.environ["CALLMAIBE_HOME"] = str(home)
    try:
        engine = tts.create(TTSCfg(variant="int8"))
        text = "Hello world, this is a test of the phone system."
        chunks = list(engine.synthesize(text))
    finally:
        del os.environ["CALLMAIBE_HOME"]
    return home, chunks


@pytest.mark.slow
def test_kokoro_then_whisper(spoken, monkeypatch):
    from callmaibe import stt

    home, chunks = spoken
    assert chunks and all(len(c) % 2 == 0 for c in chunks)
    monkeypatch.setenv("CALLMAIBE_HOME", str(home))
    text = stt.create(STTCfg(model="tiny", compute_type="int8", device="cpu")).transcribe(b"".join(chunks))
    assert "hello" in text.lower()


@pytest.mark.slow
def test_real_vad_on_kokoro(spoken):
    _, chunks = spoken
    speech = b"".join(chunks)
    speech = speech[: len(speech) // FRAME_BYTES * FRAME_BYTES]
    pcm = speech + b"\x00" * (SAMPLE_RATE * 2 * 3 // 2)
    det = TurnDetector()
    evs = [det.feed(pcm[i : i + FRAME_BYTES]) for i in range(0, len(pcm), FRAME_BYTES)]
    assert TurnEvent.SPEECH_START in evs and TurnEvent.TURN_END in evs
    assert evs.index(TurnEvent.SPEECH_START) < evs.index(TurnEvent.TURN_END)
    assert abs(len(det.utterance()) - len(speech)) <= 0.3 * len(speech)


@pytest.mark.slow
def test_time_to_first_chunk(spoken, monkeypatch, capsys):
    from callmaibe import tts

    home, _ = spoken
    monkeypatch.setenv("CALLMAIBE_HOME", str(home))
    engine = tts.create(TTSCfg(variant="int8"))
    engine.warmup()
    t = time.perf_counter()
    next(iter(engine.synthesize("I fixed the failing test. Do you want me to push it?")))
    dt = time.perf_counter() - t
    with capsys.disabled():
        print(f"\ntime-to-first-chunk: {dt:.2f}s")
