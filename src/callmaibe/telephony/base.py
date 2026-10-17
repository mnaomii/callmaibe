"""Telephony backend interface. Implementations: pjsua2 (real SIP), local (mic/speaker), fake (tests)."""

from abc import ABC, abstractmethod
from enum import StrEnum


class CallState(StrEnum):
    DIALING = "dialing"
    RINGING = "ringing"
    ACTIVE = "active"
    ENDED = "ended"


class EndReason(StrEnum):
    LOCAL_HANGUP = "local_hangup"
    REMOTE_HANGUP = "remote_hangup"
    DECLINED = "declined"        # 603 / 486 from a user tap on "decline"
    BUSY = "busy"
    NO_ANSWER = "no_answer"      # our ring timeout, or 408/480
    ERROR = "error"


class Call(ABC):
    """One outgoing call. All methods are thread-safe and never block longer than stated."""

    # False when the callee hears us through a speaker that our mic picks up (local backend);
    # the conversation engine then skips barge-in detection while it is speaking.
    full_duplex: bool = True

    @property
    @abstractmethod
    def state(self) -> CallState: ...

    @property
    @abstractmethod
    def end_reason(self) -> EndReason | None: ...

    @abstractmethod
    def wait_answered(self, timeout_s: float) -> CallState:
        """Block until ACTIVE or ENDED, or timeout. On timeout, hang up with NO_ANSWER and return ENDED."""

    @abstractmethod
    def play(self, pcm: bytes) -> None:
        """Queue PCM (internal format) to send to the callee. Returns immediately."""

    @abstractmethod
    def stop_playback(self) -> None:
        """Drop all queued outgoing audio (barge-in)."""

    @abstractmethod
    def playing(self) -> bool:
        """True while queued outgoing audio remains."""

    @abstractmethod
    def read(self, timeout_s: float) -> bytes | None:
        """Next incoming 20 ms frame (FRAME_BYTES). None on timeout or once the call has ended."""

    @abstractmethod
    def hangup(self) -> None:
        """End the call (LOCAL_HANGUP). Idempotent."""


class Telephony(ABC):
    @abstractmethod
    def start(self) -> None:
        """Initialise and register. Raises on fatal config errors."""

    @abstractmethod
    def stop(self) -> None: ...

    @property
    @abstractmethod
    def registered(self) -> bool: ...

    @abstractmethod
    def dial(self, uri: str) -> Call:
        """Start ringing `uri`. Returns immediately; use Call.wait_answered."""
