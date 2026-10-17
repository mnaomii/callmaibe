from datetime import datetime, timedelta

import pytest

from callmaibe.config import PolicyCfg, QuietRange
from callmaibe.policy import Policy

# Monday 2026-10-19 12:00
NOW = datetime(2026, 10, 19, 12, 0)


def mk(tmp_path, **kw) -> Policy:
    return Policy(PolicyCfg(**kw), tmp_path / "policy.json")


def test_default_session_off(tmp_path):
    ok, reason = mk(tmp_path).check("s", NOW)
    assert not ok and "/call on" in reason


def test_session_on_off(tmp_path):
    p = mk(tmp_path)
    p.enable_session("s", True, NOW)
    assert p.check("s", NOW) == (True, "ok")
    assert not p.check("other", NOW)[0]
    p.enable_session("s", False, NOW)
    assert not p.check("s", NOW)[0]


def test_global_off_wins(tmp_path):
    p = mk(tmp_path)
    p.enable_session("s", True, NOW)
    p.set_global(False)
    ok, reason = p.check("s", NOW)
    assert not ok and "switched off" in reason
    p.set_global(True)
    assert p.check("s", NOW)[0]


def test_cfg_enabled_false_is_default(tmp_path):
    p = mk(tmp_path, enabled=False)
    p.enable_session("s", True, NOW)
    assert not p.check("s", NOW)[0]
    p.set_global(True)
    assert p.check("s", NOW)[0]


def test_snooze(tmp_path):
    p = mk(tmp_path)
    p.enable_session("s", True, NOW)
    p.snooze(30, NOW)
    ok, reason = p.check("s", NOW + timedelta(minutes=10))
    assert not ok and "12:30" in reason
    assert p.check("s", NOW + timedelta(minutes=31))[0]
    p.clear_snooze(NOW)
    assert p.check("s", NOW)[0]


def test_decline_snooze(tmp_path):
    p = mk(tmp_path, decline_snooze_min=45)
    p.enable_session("s", True, NOW)
    p.on_declined(NOW)
    assert not p.check("s", NOW + timedelta(minutes=44))[0]
    assert p.check("s", NOW + timedelta(minutes=46))[0]


def test_once_permit(tmp_path):
    p = mk(tmp_path, once_permit_min=10)
    p.grant_once("s", NOW)
    assert p.check("s", NOW + timedelta(minutes=9))[0]
    assert not p.check("s", NOW + timedelta(minutes=11))[0]
    p.grant_once("s", NOW)
    p.consume_once("s", NOW)
    assert not p.check("s", NOW)[0]


def test_once_does_not_beat_snooze(tmp_path):
    p = mk(tmp_path)
    p.grant_once("s", NOW)
    p.snooze(60, NOW)
    assert not p.check("s", NOW)[0]


def q(days, start, end):
    return QuietRange(days=days, start=start, end=end)


@pytest.mark.parametrize("rng,when,expected", [
    (q("mon-fri", "08:00", "14:00"), NOW, True),                                  # Mon noon
    (q("mon-fri", "08:00", "14:00"), datetime(2026, 10, 24, 12, 0), False),       # Saturday
    (q("mon-fri", "08:00", "14:00"), datetime(2026, 10, 19, 14, 0), False),       # end exclusive
    (q("tue", "08:00", "14:00"), NOW, False),
    (q("sat,sun", "00:00", "23:59"), datetime(2026, 10, 25, 9, 0), True),
    (q("fri", "22:00", "07:00"), datetime(2026, 10, 23, 23, 0), True),            # Fri night
    (q("fri", "22:00", "07:00"), datetime(2026, 10, 24, 6, 0), True),             # Sat morning, start day Fri
    (q("fri", "22:00", "07:00"), datetime(2026, 10, 24, 8, 0), False),
    (q("fri", "22:00", "07:00"), datetime(2026, 10, 23, 6, 0), False),            # Fri morning belongs to Thu
    (q("mon-sun", "00:00", "00:00"), NOW, True),
    (q("fri-mon", "10:00", "11:00"), datetime(2026, 10, 25, 10, 30), True),       # wrapping day range
])
def test_quiet_hours(tmp_path, rng, when, expected):
    p = mk(tmp_path, quiet_hours=[rng])
    p.enable_session("s", True, when)
    ok, reason = p.check("s", when)
    assert ok is (not expected)
    if expected:
        assert "quiet hours" in reason
    assert p.status("s", when)["quiet_now"] is expected


def test_corrupt_file_is_empty(tmp_path):
    p = mk(tmp_path)
    p.path.write_text("{not json")
    assert not p.check("s", NOW)[0]
    p.enable_session("s", True, NOW)
    assert p.check("s", NOW)[0]
    p.path.write_text("[1, 2]")
    assert not p.check("s", NOW)[0]


def test_prune_old_sessions(tmp_path):
    p = mk(tmp_path)
    p.enable_session("old", True, NOW - timedelta(days=8))
    p.enable_session("new", True, NOW - timedelta(days=6))
    p.snooze(1, NOW)  # any write prunes
    assert not p.check("old", NOW)[0]
    assert p.check("new", NOW + timedelta(minutes=2))[0]
    assert "old" not in p.path.read_text()


def test_status_fields(tmp_path):
    p = mk(tmp_path)
    p.enable_session("s", True, NOW)
    p.snooze(15, NOW)
    st = p.status("s", NOW)
    assert st["allowed"] is False
    assert st["global_enabled"] is True
    assert st["session_enabled"] is True
    assert st["snoozed_until"] == "2026-10-19T12:15:00"
    assert st["quiet_now"] is False
    assert st["active_call"] is None


def test_reads_fresh(tmp_path):
    a, b = mk(tmp_path), mk(tmp_path)
    a.enable_session("s", True, NOW)
    assert b.check("s", NOW)[0]
    a.set_global(False)
    assert not b.check("s", NOW)[0]
