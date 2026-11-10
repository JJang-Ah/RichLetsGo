"""Historical (< B) and live orderbook collection for D103 stage 0 (M2-10 sections 2-3)."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from richletsgo.backtest.codec import encode
from richletsgo.marketdata.bybit_public import BybitApiError

from .allowlist import Allowlist
from .guard import FUNDING_WINDOW, ObservationIncident, StructureClient
from .models import AFTER, BEFORE, BOUNDARY, DEPTH_EVERY, HISTORY_INTERVALS, LIVE_SPAN, TOP_EVERY
from .sessions import Sessions, calendar_for, load
from .store import Store

PATHS = {
    "trade": "/v5/market/kline",
    "index": "/v5/market/index-price-kline",
    "mark": "/v5/market/mark-price-kline",
}


def reopen_windows(sessions: Sessions) -> list[tuple[int, int]]:
    return [(b.at + BEFORE[0], b.at + AFTER[1]) for b in sessions.boundaries()]


def _in_windows(start: int, windows: list[tuple[int, int]]) -> bool:
    return any(a <= start < b for a, b in windows)


def history_tasks(allowlist: Allowlist) -> list[tuple[str, str, str, int, int]]:
    """(kind, interval, symbol, first, last) with last = final bar start or settlement time."""
    tasks = []
    for symbol in sorted(allowlist.symbols):
        launch = allowlist.entries[symbol].launch
        if launch >= BOUNDARY:
            continue  # observed only through live orderbook samples
        for kind, interval in (("trade", "60"), ("index", "60"), ("mark", "60"), ("trade", "5")):
            width = HISTORY_INTERVALS[interval]
            tasks.append((kind, interval, symbol, launch // width * width, BOUNDARY - width))
        tasks.append(("funding", "", symbol, launch, BOUNDARY - 1))
    return tasks


def collect_history(
    store: Store,
    client: StructureClient,
    allowlist: Allowlist,
    table: dict[str, Any],
    *,
    code_commit: str,
) -> None:
    store.bind("allowlist_sha256", allowlist.sha256())
    store.log(
        {
            "event": "history_start",
            "at": datetime.now(UTC).isoformat(),
            "code_commit": code_commit,
            "boundary": BOUNDARY,
            "symbols": sorted(allowlist.symbols),
            "rejected": allowlist.rejected,
            "held": allowlist.held,
        }
    )
    windows = {
        s: reopen_windows(load(table, calendar_for(e))) for s, e in allowlist.entries.items()
    }
    for kind, interval, symbol, first, last in history_tasks(allowlist):
        step = FUNDING_WINDOW if kind == "funding" else 1000 * HISTORY_INTERVALS[interval]
        cursor = first
        while cursor <= last:
            end = min(cursor + step - (1 if kind == "funding" else step // 1000), last)
            key = encode([kind, interval, symbol, cursor, end])
            if not store.done(key):
                try:
                    if kind == "funding":
                        funding = client.funding(symbol, cursor, end)
                        store.put("funding", [(symbol, int(t), r) for t, r in funding], key)
                    else:
                        rows = client.bars(PATHS[kind], symbol, interval, cursor, end)
                        if interval == "5":
                            rows = [r for r in rows if _in_windows(int(r[0]), windows[symbol])]
                        store.put("bars", [_bar(kind, interval, symbol, r) for r in rows], key)
                except ObservationIncident as exc:
                    store.log({"event": "observation_incident", "page": key, "error": str(exc)})
                    raise
                except BybitApiError as exc:
                    store.log({"event": "missing", "page": key, "error": str(exc)})
                finally:
                    client.attempts.clear()
                    client.requests.clear()
            cursor = end + (1 if kind == "funding" else step // 1000)
    store.log({"event": "history_done", "data_hash": store.data_hash()})


def _bar(kind: str, interval: str, symbol: str, row: list[str]) -> tuple[Any, ...]:
    start, open_, high, low, close = row[:5]
    volume: str | None = row[5] if kind == "trade" else None
    turnover: str | None = row[6] if kind == "trade" else None
    return (kind, interval, symbol, int(start), open_, high, low, close, volume, turnover)


def _side(levels: Any) -> tuple[str | None, str | None]:
    if not levels:
        return None, None
    return str(levels[0][0]), str(levels[0][1])


def collect_live(
    store: Store,
    client: StructureClient,
    allowlist: Allowlist,
    *,
    clock: Callable[[], int],
    sleep: Callable[[float], None],
    stop_after: int | None = None,
) -> None:
    """Sample orderbooks on a fixed grid from T0 for 14 days; resumable by slot."""
    store.bind("allowlist_sha256", allowlist.sha256())
    t0 = store.get("T0")
    if t0 is None:
        now = clock()
        store.bind("T0", str(now))
        store.log({"event": "live_start", "T0": now, "held": allowlist.held})
        t0 = str(now)
    until = int(t0) + LIVE_SPAN
    symbols = sorted(allowlist.symbols)
    slots = 0
    while True:
        now = clock()
        slot = now // TOP_EVERY * TOP_EVERY
        if slot >= until or (stop_after is not None and slots >= stop_after):
            break
        key = encode(["live", slot])
        if not store.done(key):
            top, depth = [], []
            for symbol in symbols:
                try:
                    book = client.orderbook(symbol, "1")
                    bid, bid_qty = _side(book.get("b"))
                    ask, ask_qty = _side(book.get("a"))
                    top.append((symbol, slot, int(book["ts"]), bid, bid_qty, ask, ask_qty))
                    if slot % DEPTH_EVERY == 0:
                        full = client.orderbook(symbol, "50")
                        depth.append(
                            (symbol, slot, int(full["ts"]), encode(full["b"]), encode(full["a"]))
                        )
                except BybitApiError as exc:
                    store.log(
                        {"event": "missing", "slot": slot, "symbol": symbol, "error": str(exc)}
                    )
                finally:
                    client.attempts.clear()
                    client.requests.clear()
            store.put("book_top", top)
            store.put("book_depth", depth, key)
        slots += 1
        wait = (slot + TOP_EVERY - clock()) / 1000
        if wait > 0:
            sleep(wait)
    store.log({"event": "live_stop", "at": clock(), "data_hash": store.data_hash()})
