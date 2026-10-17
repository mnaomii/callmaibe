"""Result types shared by the conversation engine, daemon API and MCP tools."""

from dataclasses import asdict, dataclass
from enum import StrEnum


class Status(StrEnum):
    IN_CALL = "in_call"              # call is up; user_said holds the reply (may be "" if expect_reply=False)
    NO_REPLY = "no_reply"            # call is up but the user said nothing within listen_timeout_s
    ENDED_BY_USER = "ended_by_user"  # user hung up; user_said holds their last words, if any
    ENDED = "ended"                  # call_end finished, or idle/max-duration hangup
    UNAVAILABLE = "unavailable"      # policy blocked the call; reason says why
    DECLINED = "declined"            # user rejected the call (triggers decline snooze)
    NO_ANSWER = "no_answer"          # rang out after ring_timeout_s
    BUSY = "busy"                    # user's phone reported busy
    LINE_BUSY = "line_busy"          # another call is already active on the daemon
    NOT_FOUND = "not_found"          # unknown or stale call_id
    ERROR = "error"                  # anything else; reason has details


@dataclass
class TurnResult:
    status: Status
    user_said: str = ""
    call_id: str = ""
    reason: str = ""
    transcript_path: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = str(self.status)
        return d
