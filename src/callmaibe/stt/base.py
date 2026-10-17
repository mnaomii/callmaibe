from abc import ABC, abstractmethod


class STT(ABC):
    @abstractmethod
    def transcribe(self, pcm: bytes) -> str:
        """Transcribe one user turn of internal-format PCM. Returns stripped text, "" for no speech."""

    def warmup(self) -> None:
        """Load models ahead of the first call. Optional."""
