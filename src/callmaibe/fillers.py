"""Spoken fillers, so the line is never silent for long.

A short backchannel ("Yeah.") plays in a pause while you are still talking, and an
acknowledgement the moment you stop ("Got it."), while your words are transcribed.
While the agent works, the between-turns thread asks for a thinking phrase
("Let me check.") and later for "still working" ones. Each is picked at random, skipping
recent picks. Synthesizing a phrase takes about half a second, too slow to do on the spot,
so all of them are made once and cached on disk.
"""

import hashlib
import os
import random
import time
from collections import deque
from collections.abc import Callable
from itertools import chain, zip_longest

from . import paths
from .config import TTSCfg
from .tts.base import TTS

# Plain words only: Kokoro spells out "mm-hm" and "uh-huh". Neutral too: an ack plays before
# we know what you said, so nothing that agrees ("makes sense") or promises ("almost done").
BACKCHANNELS = ("Yeah.", "Right.", "Okay.", "I see.", "Yep.", "Sure.")    # short: under 0.8 s
ACKS = (
    "Got it.", "Alright.", "Gotcha.", "Understood.", "Okay, got it.", "Right, okay.",
    "Alright, got it.", "I hear you.", "Noted.", "Okay, I see.",
)
THINKING = (
    "Let me think.", "One sec.", "Let me check.", "Give me a moment.", "Let me see.",
    "Just a moment.", "Let me look into it.", "Let me think about that.", "One moment.",
    "Let me have a look.", "Give me a second.", "Let me work that out.", "Just a sec.",
    "Hold on a second.", "Let me check on that.", "Okay, let me see.",
)
STILL = (
    "Still on it.", "Still thinking.", "Bear with me.", "Hang on.", "Still working on it.",
    "Hang tight.", "Still with you.", "Just a bit longer.", "Still looking.",
    "Thanks for waiting.", "One more moment.", "Bear with me a moment.", "Still here.",
    "This is taking a moment.", "Still going.",
)


class Fillers:
    def __init__(self, tts: TTS, cfg: TTSCfg, seed: int | None = None):
        self._tts = tts
        self._cfg = cfg
        self._pcm: dict[str, bytes] = {}
        self._recent: deque[str] = deque(maxlen=8)
        self._rng = random.Random(seed)

    def backchannel(self) -> bytes | None:
        return self._pick(BACKCHANNELS)

    def ack(self) -> bytes | None:
        return self._pick(ACKS + BACKCHANNELS)

    def thinking(self, first: bool) -> bytes | None:
        return self._pick(THINKING if first else STILL)

    def _pick(self, phrases: tuple[str, ...]) -> bytes | None:
        ready = [p for p in phrases if p in self._pcm]
        fresh = [p for p in ready if p not in self._recent] or ready
        if not fresh:
            return None
        text = self._rng.choice(fresh)
        self._recent.append(text)
        return self._pcm[text]

    def render(self, busy: Callable[[], bool] = lambda: False) -> None:
        """Load or synthesize every phrase. Slow the first time, so run it in a background thread."""
        d = paths.models_dir() / "fillers"
        d.mkdir(exist_ok=True)
        c = self._cfg
        # interleaved, so a few of each kind are ready early
        groups = zip_longest(BACKCHANNELS, ACKS, THINKING, STILL)
        for text in filter(None, chain.from_iterable(groups)):
            key = repr((c.engine, c.voice, c.speed, c.variant, text)).encode()
            path = d / f"{hashlib.sha256(key).hexdigest()[:16]}.pcm"
            if not path.exists():
                while busy():               # synthesizing now would slow the call's speech and STT
                    time.sleep(1)
                pcm = b"".join(self._tts.synthesize(text))
                if not pcm:
                    continue
                part = path.with_name(f"{path.name}.{os.getpid()}.part")
                part.write_bytes(pcm)
                os.replace(part, path)
            self._pcm[text] = path.read_bytes()
