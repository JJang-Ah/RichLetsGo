"""M2-10c live coverage: schedule-slot accounting without reading prices (synthetic DB)."""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal as D
from pathlib import Path

import pytest

from richletsgo.backtest.codec import encode
from richletsgo.research.d103 import stats
from richletsgo.research.d103.allowlist import Allowlist, Entry
from richletsgo.research.d103.coverage import bound_allowlist, coverage, stamp
from richletsgo.research.d103.models import HOUR, LIVE_SPAN, MINUTE
from richletsgo.research.d103.store import Store

A, B = "XQAUSDT", "XQBUSDT"
EXPECTED = frozenset({A, B})
SLOT0 = 1000 * HOUR  # first schedule label: an hour slot that precedes T0
T0 = SLOT0 + 3 * MINUTE
STEP = 5 * MINUTE


def top(symbol: str, at: int) -> tuple[object, ...]:
    return (symbol, at, at + 1, at + 2, "9", "1", "11", "1")


def depth(symbol: str, at: int) -> tuple[object, ...]:
    return (symbol, at, at + 1, at + 2, "[]", "[]")


def checkpoint(store: Store, slot: int) -> None:
    store.put("book_depth", [], encode(["live", slot]))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "d103.sqlite")
    s.bind("T0", str(T0))
    hour1 = SLOT0 + HOUR
    last = (T0 + LIVE_SPAN - 1) // STEP * STEP
    # slot0 (hour): both tops, depth failed for B -> partial depth, B top-only.
    s.put("book_top", [top(A, SLOT0), top(B, SLOT0)])
    s.put("book_depth", [depth(A, SLOT0)])
    checkpoint(s, SLOT0)
    # slot1: B rejected (403) but the slot is checkpointed -> partial top.
    s.put("book_top", [top(A, SLOT0 + STEP)])
    checkpoint(s, SLOT0 + STEP)
    # slot2: rows kept before an incident, no checkpoint.
    s.put("book_top", [top(A, SLOT0 + 2 * STEP)])
    # hour1: B's depth was kept but its top was not -> partial top, B depth-only.
    s.put("book_top", [top(A, hour1)])
    s.put("book_depth", [depth(A, hour1), depth(B, hour1)])
    checkpoint(s, hour1)
    # A full slot right after hour1; the final (expiring) slot stays missing.
    s.put("book_top", [top(A, hour1 + STEP), top(B, hour1 + STEP)])
    checkpoint(s, hour1 + STEP)
    assert last > hour1 + STEP
    # A row before the schedule and a stranger symbol.
    s.put("book_top", [top(A, SLOT0 - STEP), top("ZZZUSDT", hour1 + STEP)])
    s.log({"event": "live_start", "T0": T0, "held": {}})
    s.log({"event": "missing", "slot": SLOT0 + STEP, "symbol": B, "error": "http 403"})
    s.log({"event": "live_expired_in_slot", "slot": last, "unsampled": [A, B]})
    s.log({"event": "history_start", "note": "not a live event"})
    yield s
    s.close()


def test_coverage_separates_checkpoints_from_actual_samples(store: Store) -> None:
    hour1 = SLOT0 + HOUR
    until = T0 + LIVE_SPAN
    result = coverage(store, EXPECTED)
    assert result["T0"] == stamp(T0) and result["until"] == stamp(until)
    assert result["schedule_slots"] == len(range(SLOT0, until, STEP))
    # The first label is on the hour and before T0, so the span holds 7 * 24 + 1 hour labels.
    assert result["depth_schedule_slots"] == 7 * 24 + 1
    assert result["checkpointed_slots"] == 4
    assert [r["slot"] for r in result["partial_top_slots"]] == [stamp(SLOT0 + STEP), stamp(hour1)]
    assert result["partial_top_slots"][0]["missing"] == [B]
    assert result["partial_depth_slots"] == [{"slot": stamp(SLOT0), "have": 1, "missing": [B]}]
    assert result["one_sided_hour_slots"] == [
        {"slot": stamp(SLOT0), "top_only": [B], "depth_only": []},
        {"slot": stamp(hour1), "top_only": [], "depth_only": [B]},
    ]
    assert result["rows_without_checkpoint"] == [
        {"slot": stamp(SLOT0 + 2 * STEP), "top": 1, "depth": 0}
    ]
    assert result["rows_outside_schedule"] == {"book_top": 1, "book_depth": 0}
    assert result["unexpected_symbols"] == {"book_top": ["ZZZUSDT"], "book_depth": []}
    ranges = result["missing_slot_ranges"]
    assert ranges[0] == {
        "start": stamp(SLOT0 + 2 * STEP),
        "end": stamp(hour1),
        "slots": (hour1 - SLOT0 - 2 * STEP) // STEP,
    }
    # The last range ends at T0 + LIVE_SPAN, not at the next 5-minute label.
    assert ranges[-1]["end"] == stamp(until)
    assert sum(r["slots"] for r in ranges) == result["schedule_slots"] - 4
    assert result["missing_events"] == [
        {"slot": stamp(SLOT0 + STEP), "error": "http 403", "symbols": 1}
    ]
    assert [e["event"] for e in result["events"]] == ["live_start", "live_expired_in_slot"]


def test_coverage_cannot_read_prices_or_sizes(store: Store) -> None:
    coverage(store, EXPECTED)
    assert list(store.rows("SELECT symbol, at FROM book_top LIMIT 1"))
    for sql in (
        "SELECT bid FROM book_top",
        "SELECT ask_qty FROM book_top",
        "SELECT * FROM book_top",
        "SELECT bids FROM book_depth",
        "SELECT start FROM bars",
        "SELECT rate FROM funding",
        "SELECT rows FROM pages",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            list(store.rows(sql))
    with pytest.raises(sqlite3.DatabaseError):
        store.data_hash()


def test_coverage_needs_t0(tmp_path: Path) -> None:
    empty = Store(tmp_path / "d103.sqlite")
    with pytest.raises(ValueError, match="T0"):
        coverage(empty, EXPECTED)
    empty.close()


def entry(symbol: str) -> Entry:
    return Entry(symbol, "stock", symbol[:-4], 0, "US", symbol[:-4], "1", "0.02", ())


def test_expected_symbols_follow_logged_held_and_bound_digest(tmp_path: Path) -> None:
    s = Store(tmp_path / "d103.sqlite")
    collected = Allowlist({A: entry(A), B: entry(B)}, held={B: "identity_changed"})
    s.bind("allowlist_sha256", collected.sha256())
    s.log({"event": "live_start", "T0": T0, "held": collected.held})
    rebuilt = bound_allowlist(s, Allowlist({A: entry(A), B: entry(B)}))
    assert rebuilt.symbols == frozenset({A})
    with pytest.raises(ValueError, match="allowlist differs"):
        bound_allowlist(s, Allowlist({A: entry(A), B: entry(B), "XQCUSDT": entry("XQCUSDT")}))
    s.close()


def test_flat_summary_reports_nearest_rank_p95() -> None:
    assert stats.flat_summary([3, 1, 10, 2]) == {
        "runs": 4,
        "max_hours": 10,
        "median_hours": D(2),
        "p95_hours": D(10),
    }
    assert stats.flat_summary([]) == {
        "runs": 0,
        "max_hours": 0,
        "median_hours": None,
        "p95_hours": None,
    }
