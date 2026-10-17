"""Availability policy: global switch, per-session switch, once-permits, snooze, quiet hours.

State lives in one JSON file that is re-read on every call, so the CLI, the hook and the
daemon (separate processes) always agree and toggles work while the daemon is down.
"""

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import paths
from .config import PolicyCfg, QuietRange

SESSION_TTL = timedelta(days=7)
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_lock = threading.RLock()


def _naive(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now()
    return now.astimezone().replace(tzinfo=None) if now.tzinfo else now


def _parse_days(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.lower().replace(" ", "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        i = _DAYS.index(a[:3])
        j = _DAYS.index(b[:3]) if b else i
        while True:
            out.add(i)
            if i == j:
                break
            i = (i + 1) % 7
    return out


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_range(r: QuietRange, now: datetime) -> bool:
    days = _parse_days(r.days)
    start, end = _hm(r.start), _hm(r.end)
    t = now.hour * 60 + now.minute
    today = now.weekday()
    if start == end:
        return today in days
    if start < end:
        return today in days and start <= t < end
    return (today in days and t >= start) or ((today - 1) % 7 in days and t < end)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(s: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


class Policy:
    def __init__(self, cfg: PolicyCfg, path: Path | None = None):
        self.cfg = cfg
        self.path = path or paths.policy_file()

    # state io
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError
        except (OSError, ValueError, TypeError):
            data = {}
        data.setdefault("global_enabled", None)
        for k in ("sessions", "once"):
            if not isinstance(data.get(k), dict):
                data[k] = {}
        data.setdefault("snoozed_until", None)
        return data

    def _save(self, state: dict, now: datetime) -> None:
        cutoff = now - SESSION_TTL
        state["sessions"] = {
            k: v for k, v in state["sessions"].items()
            if isinstance(v, dict) and (_parse(v.get("updated")) or now) >= cutoff
        }
        state["once"] = {k: v for k, v in state["once"].items() if (_parse(v) or now) > now}
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _mutate(self, now: datetime | None, fn) -> None:
        now = _naive(now)
        with _lock:
            state = self._load()
            fn(state, now)
            self._save(state, now)

    # queries
    def _quiet(self, now: datetime) -> QuietRange | None:
        for r in self.cfg.quiet_hours:
            if in_range(r, now):
                return r
        return None

    def _global(self, state: dict) -> bool:
        g = state["global_enabled"]
        return self.cfg.enabled if g is None else bool(g)

    def _snooze_until(self, state: dict, now: datetime) -> datetime | None:
        until = _parse(state["snoozed_until"]) if state["snoozed_until"] else None
        return until if until and until > now else None

    def _once_valid(self, state: dict, key: str, now: datetime) -> bool:
        exp = _parse(state["once"].get(key))
        return bool(exp and exp > now)

    def check(self, session: str, now: datetime | None = None) -> tuple[bool, str]:
        now = _naive(now)
        state = self._load()
        if not self._global(state):
            return False, "calling is switched off (the user can run `callmaibe on`)"
        until = self._snooze_until(state, now)
        if until:
            return False, f"calling is snoozed until {until:%H:%M} ({until:%a %d %b})"
        q = self._quiet(now)
        if q:
            return False, f"quiet hours ({q.days} {q.start}-{q.end})"
        if state["sessions"].get(session, {}).get("enabled") or self._once_valid(state, session, now):
            return True, "ok"
        return False, "calling is off for this session (the user can type /call on)"

    def status(self, session: str, now: datetime | None = None) -> dict:
        now = _naive(now)
        state = self._load()
        allowed, reason = self.check(session, now)
        until = self._snooze_until(state, now)
        return {
            "allowed": allowed,
            "reason": reason,
            "global_enabled": self._global(state),
            "session_enabled": bool(state["sessions"].get(session, {}).get("enabled")),
            "once_permit": self._once_valid(state, session, now),
            "snoozed_until": _iso(until) if until else None,
            "quiet_now": self._quiet(now) is not None,
            "active_call": None,
        }

    # toggles
    def set_global(self, on: bool, now: datetime | None = None) -> None:
        self._mutate(now, lambda s, n: s.update(global_enabled=bool(on)))

    def enable_session(self, key: str, on: bool, now: datetime | None = None) -> None:
        self._mutate(now, lambda s, n: s["sessions"].__setitem__(
            key, {"enabled": bool(on), "updated": _iso(n)}))

    def grant_once(self, key: str, now: datetime | None = None) -> None:
        self._mutate(now, lambda s, n: s["once"].__setitem__(
            key, _iso(n + timedelta(minutes=self.cfg.once_permit_min))))

    def consume_once(self, key: str, now: datetime | None = None) -> None:
        self._mutate(now, lambda s, n: s["once"].pop(key, None))

    def snooze(self, minutes: float, now: datetime | None = None) -> None:
        self._mutate(now, lambda s, n: s.update(snoozed_until=_iso(n + timedelta(minutes=minutes))))

    def clear_snooze(self, now: datetime | None = None) -> None:
        self._mutate(now, lambda s, n: s.update(snoozed_until=None))

    def on_declined(self, now: datetime | None = None) -> None:
        self.snooze(self.cfg.decline_snooze_min, now)
