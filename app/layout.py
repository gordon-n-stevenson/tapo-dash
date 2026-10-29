"""Dashboard-side names and card order, set from the page's edit mode.

The Tapo app cannot name some things (a P300 strip as a whole, plugs paired to
another account), so names set here override whatever the device reports.
Keys are "MAC" for a plug or strip and "MAC|socket id" for one strip socket,
so both survive DHCP changes.
"""
from __future__ import annotations

import sqlite3

MAX_NAME = 64


def init(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS layout (
               key TEXT PRIMARY KEY,
               name TEXT,
               position INTEGER
           )"""
    )
    conn.commit()


def key(mac: str, child_id: str | None = None) -> str:
    return f"{mac}|{child_id}" if child_id else mac


def names(conn: sqlite3.Connection) -> dict[str, str]:
    cur = conn.execute("SELECT key, name FROM layout WHERE name IS NOT NULL")
    return dict(cur.fetchall())


def positions(conn: sqlite3.Connection) -> dict[str, int]:
    cur = conn.execute("SELECT key, position FROM layout WHERE position IS NOT NULL")
    return dict(cur.fetchall())


def set_name(conn: sqlite3.Connection, k: str, name: str | None) -> str | None:
    """Store a custom name; blank or None clears it. Returns what was stored."""
    name = " ".join((name or "").split())[:MAX_NAME] or None
    conn.execute(
        "INSERT INTO layout (key, name) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET name = excluded.name",
        (k, name),
    )
    conn.commit()
    return name


def set_order(conn: sqlite3.Connection, keys: list[str]) -> None:
    """Make `keys` the full card order; anything not listed loses its place."""
    conn.execute("UPDATE layout SET position = NULL")
    conn.executemany(
        "INSERT INTO layout (key, position) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET position = excluded.position",
        [(k, i) for i, k in enumerate(dict.fromkeys(keys))],
    )
    conn.commit()


def sort_key(positions: dict[str, int], k: str, name: str) -> tuple:
    """Placed cards first in their saved order, then the rest by name."""
    pos = positions.get(k)
    return (pos is None, pos if pos is not None else 0, name.casefold())
