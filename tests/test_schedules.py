import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from app import schedules

TZ = timezone(timedelta(hours=11))
# 2026-11-02 is a Monday.
MON = date(2026, 11, 2)
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
        (MON_0800 - timedelta(seconds=1), None),
        (MON_0800, MON),
        (MON_0800 + schedules.GRACE - timedelta(seconds=1), MON),
        (MON_0800 + schedules.GRACE, None),
        (MON_0800 + timedelta(hours=6), None),
    ],
)
def test_due_only_inside_grace_window(now, due):
    assert schedules.due_on(rule(), now) == due


def test_not_due_twice_on_the_same_day():
    assert schedules.due_on(rule(last_fired="2026-11-02"), MON_0800) is None
    assert schedules.due_on(rule(last_fired="2026-11-01"), MON_0800) == MON


def test_grace_window_carries_past_midnight():
    late = rule(at="23:55", days="0")  # Mondays only
    tue_0002 = datetime(2026, 11, 3, 0, 2, tzinfo=TZ)
    assert schedules.due_on(late, tue_0002) == MON
    assert schedules.due_on({**late, "last_fired": "2026-11-02"}, tue_0002) is None
    assert schedules.due_on(late, tue_0002 + schedules.GRACE) is None


def test_respects_weekdays():
    weekends = rule(days="56")
    assert schedules.due_on(weekends, MON_0800) is None
    assert schedules.due_on(weekends, MON_0800 + timedelta(days=5)) == date(2026, 11, 7)


def test_disabled_rule_never_due():
    assert schedules.due_on(rule(enabled=0), MON_0800) is None


@pytest.mark.parametrize(
    "action, at, days",
    [("dim", "08:00", "0"), ("on", "8:00", "0"), ("on", "24:00", "0"),
     ("on", "08:60", "0"), ("on", "08:00\n", "0"), ("on", "08:00", ""),
     ("on", "08:00", "7")],
)
def test_validate_rejects_bad_input(action, at, days):
    with pytest.raises(ValueError):
        schedules.validate(action, at, days)


def test_validate_normalises_days():
    assert schedules.validate("off", "23:59", "4021433") == "01234"


def test_rule_added_inside_its_window_waits_until_next_day(db):
    late = schedules.add(db, "AA", None, "on", "08:00", "0123456", MON_0800 + timedelta(minutes=1))
    assert late["last_fired"] == "2026-11-02"
    assert schedules.due_on(late, MON_0800 + timedelta(minutes=2)) is None

    early = schedules.add(db, "AA", None, "on", "08:00", "0123456", MON_0800 - timedelta(minutes=1))
    assert early["last_fired"] is None
    assert schedules.due_on(early, MON_0800) == MON


def test_resuming_inside_window_does_not_fire_immediately(db):
    r = schedules.add(db, "AA", None, "off", "08:00", "0123456", MON_0800 - timedelta(hours=1))
    schedules.set_enabled(db, r["id"], False, MON_0800 - timedelta(minutes=30))
    schedules.set_enabled(db, r["id"], True, MON_0800 + timedelta(minutes=7))
    resumed = schedules.get(db, r["id"])
    assert resumed["enabled"] == 1
    assert schedules.due_on(resumed, MON_0800 + timedelta(minutes=8)) is None
    assert schedules.due_on(resumed, MON_0800 + timedelta(days=1)) == date(2026, 11, 3)


def test_resuming_before_the_time_still_fires_today(db):
    r = schedules.add(db, "AA", None, "off", "08:00", "0123456", MON_0800 - timedelta(hours=2))
    schedules.set_enabled(db, r["id"], False, MON_0800 - timedelta(hours=1))
    schedules.set_enabled(db, r["id"], True, MON_0800 - timedelta(minutes=5))
    assert schedules.due_on(schedules.get(db, r["id"]), MON_0800) == MON


def test_crud_and_record(db):
    r = schedules.add(db, "AA", "child-1", "off", "18:30", "01234", MON_0800)
    assert schedules.list_all(db) == [r]
    assert r["child_id"] == "child-1" and r["enabled"] == 1

    assert schedules.set_enabled(db, r["id"], False, MON_0800)
    assert schedules.get(db, r["id"])["enabled"] == 0

    schedules.record(db, r["id"], None, "failed: timed out")
    got = schedules.get(db, r["id"])
    assert got["last_result"] == "failed: timed out" and got["last_fired"] is None

    schedules.record(db, r["id"], "2026-11-02", "ok")
    assert schedules.get(db, r["id"])["last_fired"] == "2026-11-02"

    assert schedules.delete(db, r["id"])
    assert not schedules.delete(db, r["id"])
    assert not schedules.set_enabled(db, r["id"], True, MON_0800)
