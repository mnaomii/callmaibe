"""Spoken fillers: random picks without quick repeats, made once and cached on disk."""

from itertools import pairwise

import pytest

from callmaibe import fillers
from callmaibe.config import TTSCfg
from callmaibe.fillers import Fillers
from callmaibe.tts.base import TTS

ALL = fillers.BACKCHANNELS + fillers.ACKS + fillers.HEARD_YOU + fillers.THINKING + fillers.STILL


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


@pytest.mark.parametrize(("text", "check"), [
    ("Can you hear me?", True),
    ("Hello? Can you hear me?", True),
    ("Hello?", True),
    ("Hello, hello?", True),
    ("Hey, are you still there?", True),
    ("Claude, can you hear me now?", True),
    ("Okay, but can you hear me?", True),
    ("Actually, can you hear me?", True),
    ("Can you hear me? Can you hear me?", True),
    ("I was putting something in the oven and can you hear me?", False),
    ("Can you hear me? I want to change the plan.", False),
    ("Hi.", False),
    ("Can I see you?", False),
    ("Yes, go ahead.", False),
])
def test_asks_if_heard(text, check):
    assert fillers.asks_if_heard(text) is check


@pytest.mark.parametrize(("text", "only"), [
    ("Uh", True), ("Um...", True), ("Uh, um.", True), ("Hmm.", True),
    ("Mm-hmm.", False),                 # that one means yes
    ("Uh, push it.", False), ("No.", False),
])
def test_hesitation(text, only):
    assert fillers.hesitation(text) is only


def test_is_ours():
    assert fillers.is_ours("Still on it.") and fillers.is_ours("let me check")
    assert not fillers.is_ours("Let me check the tests first.")


@pytest.mark.parametrize(("text", "check"), [
    ("Okay, so I'm putting something in the oven. Can you hear me?", True),
    ("I ate lasagna and can you hear me now?", True),
    ("Okay, so can you hit me?", True),
    ("Right, and you hear me?", True),
    ("Wait, are you still with me?", True),
    ("Did you hear the news today?", False),
    ("Can you hear me if I talk from the kitchen?", False),
    ("Tell them I said hello.", False),
])
def test_ends_asking_if_heard(text, check):
    assert fillers.ends_asking_if_heard(text) is check
