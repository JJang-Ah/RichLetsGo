"""Live-sample coverage of the D103 store without reading any price or size (M2-10c).

Only identity, live checkpoint keys in pages, (symbol, at) of book_top/book_depth and the
collection log are read. An SQLite authorizer refuses every other data column, so a query
that touches prices fails instead of silently reading them. Store.data_hash() reads every
price column and belongs to the stats step, never here.
"""

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from richletsgo.backtest.codec import encode

from .allowlist import Allowlist
from .models import DEPTH_EVERY, LIVE_SPAN, TOP_EVERY
from .store import Store

READABLE = {
    "identity": frozenset({"key", "value"}),
    "pages": frozenset({"key"}),
    "book_top": frozenset({"symbol", "at"}),
    "book_depth": frozenset({"symbol", "at"}),
    "log": frozenset({"id", "content"}),
}
UNREADABLE = frozenset({"bars", "funding"})
KST = timezone(timedelta(hours=9))
# Price-free events the live collector writes; anything else in the log is not copied.
EVENTS = ("live_start", "observation_incident", "live_expired_in_slot")


def restrict(db: sqlite3.Connection) -> None:
    """Deny reads of price/size columns and of the bar and funding tables."""

    def authorize(action: int, table: str | None, column: str | None, *_: object) -> int:
        if action == sqlite3.SQLITE_READ and table is not None:
            if table in UNREADABLE:
                return sqlite3.SQLITE_DENY
            allowed = READABLE.get(table)
            if allowed is not None and column not in allowed:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db.set_authorizer(authorize)


def bound_allowlist(store: Store, allowlist: Allowlist) -> Allowlist:
    """Re-apply the held set logged at collection; the digest must match the bound one."""
    for (content,) in store.rows("SELECT content FROM log ORDER BY id"):
        event = json.loads(content)
        if "held" in event:
            allowlist.held = dict(event["held"])
    if allowlist.sha256() != store.get("allowlist_sha256"):
        raise ValueError("allowlist differs from the one bound at collection")
    return allowlist


def stamp(ms: int) -> dict[str, str]:
    at = datetime.fromtimestamp(ms / 1000, UTC)
    return {
        "utc": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kst": at.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
    }


def _ranges(slots: list[int], until: int) -> list[dict[str, Any]]:
    """Merge consecutive schedule slots; the end is clipped to T0 + LIVE_SPAN."""
    out: list[dict[str, Any]] = []
    for slot in slots:
        if out and out[-1]["_last"] + TOP_EVERY == slot:
            out[-1]["_last"] = slot
            out[-1]["slots"] += 1
        else:
            out.append({"_first": slot, "_last": slot, "slots": 1})
    return [
        {
            "start": stamp(r["_first"]),
            "end": stamp(min(r["_last"] + TOP_EVERY, until)),
            "slots": r["slots"],
        }
        for r in out
    ]


def coverage(store: Store, expected: frozenset[str]) -> dict[str, Any]:
    """Coverage of the live schedule [floor(T0, 5m), T0 + LIVE_SPAN) by slot label.

    Slot labels are schedule labels, not request times: the first label may precede T0.
    A completed checkpoint is not full coverage: a slot is checkpointed even when some
    symbols failed, and a top book is kept when its depth request failed.
    """
    restrict(store.db)
    t0_text = store.get("T0")
    if t0_text is None:
        raise ValueError("no T0 bound: live collection never started")
    t0 = int(t0_text)
    until = t0 + LIVE_SPAN
    schedule = list(range(t0 // TOP_EVERY * TOP_EVERY, until, TOP_EVERY))
    keys = {encode(["live", slot]): slot for slot in schedule}
    done = {keys[k] for (k,) in store.rows("SELECT key FROM pages") if k in keys}
    books: dict[str, dict[int, set[str]]] = {}
    outside: dict[str, int] = {}
    unexpected: dict[str, set[str]] = {}
    in_schedule = set(schedule)
    for table in ("book_top", "book_depth"):
        by_slot: dict[int, set[str]] = defaultdict(set)
        stray = 0
        strangers: set[str] = set()
        for symbol, at in store.rows(f"SELECT symbol, at FROM {table}"):
            if int(at) not in in_schedule:
                stray += 1
                continue
            if symbol not in expected:
                strangers.add(str(symbol))
            by_slot[int(at)].add(str(symbol))
        books[table], outside[table], unexpected[table] = by_slot, stray, strangers

    def short(slot: int, table: str) -> dict[str, Any] | None:
        missing = sorted(expected - books[table].get(slot, set()))
        if not missing:
            return None
        return {"slot": stamp(slot), "have": len(expected) - len(missing), "missing": missing}

    partial_top, partial_depth, one_sided = [], [], []
    for slot in sorted(done):
        if (row := short(slot, "book_top")) is not None:
            partial_top.append(row)
        if slot % DEPTH_EVERY == 0:
            if (row := short(slot, "book_depth")) is not None:
                partial_depth.append(row)
            top, depth = books["book_top"].get(slot, set()), books["book_depth"].get(slot, set())
            if top != depth:
                one_sided.append(
                    {
                        "slot": stamp(slot),
                        "top_only": sorted(top - depth),
                        "depth_only": sorted(depth - top),
                    }
                )
    unchecked = [
        {
            "slot": stamp(slot),
            "top": len(books["book_top"].get(slot, ())),
            "depth": len(books["book_depth"].get(slot, ())),
        }
        for slot in schedule
        if slot not in done and (books["book_top"].get(slot) or books["book_depth"].get(slot))
    ]
    # Live failures carry a slot; history failures carry a page key instead. Only live
    # failures on a schedule label are counted per slot; the rest are counted apart.
    missing_events: Counter[tuple[int, str]] = Counter()
    missing_other = {"history": 0, "outside_schedule": 0}
    events: list[dict[str, Any]] = []
    for (content,) in store.rows("SELECT content FROM log ORDER BY id"):
        event = json.loads(content)
        if event.get("event") == "missing":
            slot = event.get("slot")
            if slot is None:
                missing_other["history"] += 1
            elif int(slot) not in in_schedule:
                missing_other["outside_schedule"] += 1
            else:
                missing_events[(int(slot), str(event.get("error", "")))] += 1
        elif event.get("event") in EVENTS:
            events.append(event)
    return {
        "T0": stamp(t0),
        "until": stamp(until),
        "expected_symbols": len(expected),
        "schedule_slots": len(schedule),
        "depth_schedule_slots": sum(slot % DEPTH_EVERY == 0 for slot in schedule),
        "checkpointed_slots": len(done),
        "missing_slot_ranges": _ranges([s for s in schedule if s not in done], until),
        "partial_top_slots": partial_top,
        "partial_depth_slots": partial_depth,
        "one_sided_hour_slots": one_sided,
        "rows_without_checkpoint": unchecked,
        "rows_outside_schedule": outside,
        "unexpected_symbols": {k: sorted(v) for k, v in unexpected.items()},
        "missing_events": [
            {"slot": stamp(slot), "error": error, "symbols": count}
            for (slot, error), count in sorted(missing_events.items())
        ],
        "missing_events_not_counted": missing_other,
        "events": events,
    }
