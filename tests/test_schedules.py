import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import schedules

TZ = timezone(timedelta(hours=11))
# 2026-11-02 is a Monday.
MON_0800 = datetime(2026, 11, 2, 8, 0, tzinfo=TZ)


def rule(**over):
    base = {"enabled": 1, "days": schedules.ALL_DAYS, "at": "08:00", "last_fired": None}
    return {**base, **over}


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    schedules.init(conn)
    yield conn
    conn.close()


@pytest.mark.parametrize(
    "now, due",
    [
        (MON_0800 - timedelta(seconds=1), False),
        (MON_0800, True),
        (MON_0800 + schedules.GRACE - timedelta(seconds=1), True),
        (MON_0800 + schedules.GRACE, False),
        (MON_0800 + timedelta(hours=6), False),
    ],
)
def test_due_only_inside_grace_window(now, due):
    assert schedules.is_due(rule(), now) is due


def test_not_due_twice_on_the_same_day():
    assert not schedules.is_due(rule(last_fired="2026-11-02"), MON_0800)
    assert schedules.is_due(rule(last_fired="2026-11-01"), MON_0800)


def test_respects_weekdays():
    weekends = rule(days="56")
    assert not schedules.is_due(weekends, MON_0800)
    assert schedules.is_due(weekends, MON_0800 + timedelta(days=5))  # Saturday


def test_disabled_rule_never_due():
    assert not schedules.is_due(rule(enabled=0), MON_0800)


@pytest.mark.parametrize(
    "action, at, days",
    [("dim", "08:00", "0"), ("on", "8:00", "0"), ("on", "24:00", "0"),
     ("on", "08:60", "0"), ("on", "08:00", ""), ("on", "08:00", "7")],
)
def test_validate_rejects_bad_input(action, at, days):
    with pytest.raises(ValueError):
        schedules.validate(action, at, days)


def test_validate_normalises_days():
    assert schedules.validate("off", "23:59", "4021433") == "01234"


def test_rule_added_after_its_time_waits_until_next_day(db):
    late = schedules.add(db, "AA", None, "on", "08:00", "0123456", MON_0800 + timedelta(minutes=1))
    assert late["last_fired"] == "2026-11-02"
    assert not schedules.is_due(late, MON_0800 + timedelta(minutes=2))

    early = schedules.add(db, "AA", None, "on", "08:00", "0123456", MON_0800 - timedelta(minutes=1))
    assert early["last_fired"] is None
    assert schedules.is_due(early, MON_0800)


def test_crud_and_record(db):
    r = schedules.add(db, "AA", "child-1", "off", "18:30", "01234", MON_0800)
    assert schedules.list_all(db) == [r]
    assert r["child_id"] == "child-1" and r["enabled"] == 1

    assert schedules.set_enabled(db, r["id"], False)
    assert schedules.get(db, r["id"])["enabled"] == 0

    schedules.record(db, r["id"], None, "failed: timed out")
    got = schedules.get(db, r["id"])
    assert got["last_result"] == "failed: timed out" and got["last_fired"] is None

    schedules.record(db, r["id"], "2026-11-02", "ok")
    assert schedules.get(db, r["id"])["last_fired"] == "2026-11-02"

    assert schedules.delete(db, r["id"])
    assert not schedules.delete(db, r["id"])
    assert not schedules.set_enabled(db, r["id"], True)
