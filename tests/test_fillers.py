"""Spoken fillers: random picks without quick repeats, made once and cached on disk."""

from itertools import pairwise

from callmaibe import fillers
from callmaibe.config import TTSCfg
from callmaibe.fillers import Fillers
from callmaibe.tts.base import TTS

ALL = fillers.BACKCHANNELS + fillers.ACKS + fillers.THINKING + fillers.STILL


class CountingTTS(TTS):
    def __init__(self):
        self.made = []

    def synthesize(self, text):
        self.made.append(text)
        yield text.encode()


def test_render_then_pick_without_quick_repeats(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLMAIBE_HOME", str(tmp_path))
    f = Fillers(CountingTTS(), TTSCfg(), seed=1)
    assert f.ack() is None                          # nothing made yet
    f.render()
    acks = [f.ack().decode() for _ in range(50)]
    assert set(acks) <= set(fillers.ACKS + fillers.BACKCHANNELS) and len(set(acks)) > 8
    assert all(a != b for a, b in pairwise(acks))
    assert f.backchannel().decode() in fillers.BACKCHANNELS
    assert f.thinking(first=True).decode() in fillers.THINKING
    assert f.thinking(first=False).decode() in fillers.STILL


def test_render_uses_the_disk_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLMAIBE_HOME", str(tmp_path))
    first = CountingTTS()
    Fillers(first, TTSCfg()).render()
    assert sorted(first.made) == sorted(ALL)
    again = CountingTTS()
    f = Fillers(again, TTSCfg())
    f.render()
    assert again.made == [] and f.ack() is not None
    other_voice = CountingTTS()
    Fillers(other_voice, TTSCfg(voice="am_adam")).render()
    assert sorted(other_voice.made) == sorted(ALL)  # another voice gets its own recordings


def test_render_waits_while_a_call_is_active(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLMAIBE_HOME", str(tmp_path))
    waits = []
    monkeypatch.setattr(fillers.time, "sleep", waits.append)
    busy = iter([True, True])
    tts = CountingTTS()
    Fillers(tts, TTSCfg()).render(busy=lambda: next(busy, False))
    assert waits == [1, 1] and len(tts.made) == len(ALL)
