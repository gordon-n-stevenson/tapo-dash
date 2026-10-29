import sqlite3

import pytest

from app import layout


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    layout.init(conn)
    yield conn
    conn.close()


def test_key():
    assert layout.key("AA") == "AA"
    assert layout.key("AA", "c1") == "AA|c1"


def test_set_and_clear_name(db):
    assert layout.set_name(db, "AA", "  Bench   strip ") == "Bench strip"
    assert layout.names(db) == {"AA": "Bench strip"}
    assert layout.set_name(db, "AA", "   ") is None
    assert layout.names(db) == {}


def test_name_is_truncated(db):
    assert len(layout.set_name(db, "AA", "x" * 200)) == layout.MAX_NAME


def test_order_replaces_previous_and_keeps_names(db):
    layout.set_name(db, "BB", "Lamp")
    layout.set_order(db, ["BB", "AA", "CC"])
    assert layout.positions(db) == {"BB": 0, "AA": 1, "CC": 2}

    layout.set_order(db, ["CC", "AA", "CC"])  # duplicates keep their first place
    assert layout.positions(db) == {"CC": 0, "AA": 1}
    assert layout.names(db) == {"BB": "Lamp"}


def test_sort_key_puts_placed_cards_first():
    pos = {"B": 0, "A": 1}
    cards = [("A", "zeta"), ("C", "beta"), ("B", "omega"), ("D", "Alpha")]
    ordered = sorted(cards, key=lambda c: layout.sort_key(pos, c[0], c[1]))
    assert [k for k, _ in ordered] == ["B", "A", "D", "C"]
