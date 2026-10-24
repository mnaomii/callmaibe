from abc import ABC, abstractmethod
from collections.abc import Iterator


class TTS(ABC):
    @abstractmethod
    def synthesize(self, text: str) -> Iterator[bytes]:
        """Yield internal-format PCM (16 kHz mono s16le), one chunk per sentence, as soon as each is ready."""

    def stop(self) -> None:
        """End the synthesis in progress early, from another thread. Optional."""

    def warmup(self) -> None:
        """Load models ahead of the first call. Optional."""
