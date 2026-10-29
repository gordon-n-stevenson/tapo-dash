"""Time-of-day rules that switch a plug, a whole strip, or one strip socket.

Rules live in the same SQLite file as the power history and are evaluated by
the background poll loop, so they run whether or not anyone has the page open.
Times are wall-clock times in the container's timezone (set TZ).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any

# A rule still fires this long after its time if the plug was unreachable or
# the container was restarting, but no later, so a restart never replays a
# day's worth of switching.
GRACE = timedelta(minutes=10)
ALL_DAYS = "0123456"  # Monday=0 .. Sunday=6, as datetime.weekday()

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_COLUMNS = ("id", "mac", "child_id", "action", "at", "days", "enabled",
            "last_fired", "last_result")


def init(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schedules (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               mac TEXT NOT NULL,
               child_id TEXT,
               action TEXT NOT NULL,
               at TEXT NOT NULL,
               days TEXT NOT NULL,
               enabled INTEGER NOT NULL DEFAULT 1,
               last_fired TEXT,
               last_result TEXT
           )"""
    )
    conn.commit()


def validate(action: str, at: str, days: str) -> str:
    """Check a rule's fields and return the days in canonical order."""
    if action not in ("on", "off"):
        raise ValueError("action must be 'on' or 'off'")
    if not _TIME_RE.fullmatch(at or ""):
        raise ValueError("at must be HH:MM (24-hour)")
    if not days or any(d not in ALL_DAYS for d in days):
        raise ValueError("days must be digits 0-6 (Monday=0), at least one")
    return "".join(sorted(set(days)))


def fire_time(at: str, day: date, tz: Any) -> datetime:
    hour, minute = (int(x) for x in at.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=tz)


def due_on(rule: dict[str, Any], now: datetime) -> date | None:
    """The day whose run of this rule is due right now, or None.

    Yesterday is checked too, so a 23:55 rule keeps its retry window past
    midnight. Known limit: a time inside the hour skipped when DST starts
    does not fire that day.
    """
    if not rule["enabled"]:
        return None
    for day in (now.date(), now.date() - timedelta(days=1)):
        if str(day.weekday()) not in rule["days"]:
            continue
        if rule["last_fired"] and rule["last_fired"] >= day.isoformat():
            continue
        at = fire_time(rule["at"], day, now.tzinfo)
        if at <= now < at + GRACE:
            return day
    return None


def list_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM schedules ORDER BY at, id")
    return [dict(zip(_COLUMNS, row)) for row in cur.fetchall()]


def get(conn: sqlite3.Connection, rule_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM schedules WHERE id = ?", (rule_id,)
    ).fetchone()
    return dict(zip(_COLUMNS, row)) if row else None


def add(
    conn: sqlite3.Connection,
    mac: str,
    child_id: str | None,
    action: str,
    at: str,
    days: str,
    now: datetime,
) -> dict[str, Any]:
    days = validate(action, at, days)
    # A rule created inside its own grace window waits until its next day
    # rather than firing straight away.
    due = due_on({"enabled": 1, "at": at, "days": days, "last_fired": None}, now)
    last_fired = due.isoformat() if due else None
    cur = conn.execute(
        "INSERT INTO schedules (mac, child_id, action, at, days, last_fired)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (mac, child_id or None, action, at, days, last_fired),
    )
    conn.commit()
    return get(conn, cur.lastrowid)


def set_enabled(
    conn: sqlite3.Connection, rule_id: int, enabled: bool, now: datetime
) -> bool:
    rule = get(conn, rule_id)
    if rule is None:
        return False
    last_fired = rule["last_fired"]
    if enabled and not rule["enabled"]:
        # Same as add(): resuming inside the grace window must not switch
        # the plug immediately, so treat that run as already done.
        due = due_on({**rule, "enabled": 1}, now)
        if due:
            last_fired = due.isoformat()
    conn.execute(
        "UPDATE schedules SET enabled = ?, last_fired = ? WHERE id = ?",
        (1 if enabled else 0, last_fired, rule_id),
    )
    conn.commit()
    return True


def delete(conn: sqlite3.Connection, rule_id: int) -> bool:
    cur = conn.execute("DELETE FROM schedules WHERE id = ?", (rule_id,))
    conn.commit()
    return cur.rowcount > 0


def record(
    conn: sqlite3.Connection, rule_id: int, fired_on: str | None, result: str
) -> None:
    """Store the outcome of a run; fired_on=None leaves the rule due for a retry."""
    if fired_on is None:
        conn.execute("UPDATE schedules SET last_result = ? WHERE id = ?", (result, rule_id))
    else:
        conn.execute(
            "UPDATE schedules SET last_fired = ?, last_result = ? WHERE id = ?",
            (fired_on, result, rule_id),
        )
    conn.commit()
