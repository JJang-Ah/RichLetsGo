"""Design-only paginated collection. No Symbol migration or order access."""

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from richletsgo.backtest.codec import encode
from richletsgo.marketdata.bybit_public import BybitPublicClient, Transport

from .boundaries import BTC_START, DAY, END, HOUR, design_contracts, window
from .store import Store, dataset_lock


class DesignTransport:
    def __init__(self, inner: Transport, allowed: set[str]) -> None:
        self.inner = inner
        self.allowed = frozenset(allowed)

    def get(self, url: str, timeout: float) -> tuple[int, bytes]:
        u = urlsplit(url)
        pairs = parse_qsl(u.query, keep_blank_values=True)
        p = dict(pairs)
        if u.scheme != "https" or u.netloc != "api.bybit.com" or u.fragment or len(p) != len(pairs):
            raise ValueError("unsupported endpoint")
        if p.get("category") != "linear" or p.get("symbol") not in self.allowed:
            raise ValueError("not a design symbol")
        if u.path == "/v5/market/kline":
            if (
                set(p) != {"category", "symbol", "interval", "start", "end", "limit"}
                or p["interval"] not in {"60", "D"}
                or p["limit"] != "1000"
            ):
                raise ValueError("unsupported kline request")
            if p["interval"] == "D" and p["symbol"] != "BTCUSDT":
                raise ValueError("daily input is BTC only")
        elif u.path == "/v5/market/funding/history":
            if (
                set(p) != {"category", "symbol", "startTime", "endTime", "limit"}
                or p["limit"] != "200"
            ):
                raise ValueError("unsupported funding request")
        else:
            raise ValueError("endpoint forbidden")
        return self.inner.get(url, timeout)


class DesignClient(BybitPublicClient):
    def page(self, symbol: str, kind: str, start: int, end: int, allowed: set[str]) -> list[Any]:
        if symbol not in allowed:
            raise ValueError("not a design symbol")
        p = {"category": "linear", "symbol": symbol}
        if kind == "funding":
            path = "/v5/market/funding/history"
            p.update(startTime=str(start), endTime=str(end), limit="200")
        else:
            if kind not in {"bars", "daily"} or (kind == "daily" and symbol != "BTCUSDT"):
                raise ValueError("invalid series")
            path = "/v5/market/kline"
            p.update(
                start=str(start),
                end=str(end),
                interval="D" if kind == "daily" else "60",
                limit="1000",
            )
        result = self._get(path, p)
        if result.get("category", "linear") != "linear" or result.get("symbol", symbol) != symbol:
            raise ValueError("response identity mismatch")
        rows = result.get("list")
        if not isinstance(rows, list):
            raise ValueError("missing response rows")
        if kind == "funding":
            if any(r.get("symbol") != symbol for r in rows):
                raise ValueError("funding response identity mismatch")
            return [[r["fundingRateTimestamp"], r["fundingRate"]] for r in rows]
        return rows


def _collect(
    pool: dict[str, Any],
    store: Store,
    client: DesignClient,
    *,
    symbols: list[str] | None = None,
    refresh: bool = False,
    code_commit: str | None = None,
) -> None:
    contracts = design_contracts(pool)
    selected = sorted(contracts) if symbols is None else sorted(set(symbols))
    if not set(selected) <= contracts.keys() or "BTCUSDT" not in contracts:
        raise ValueError("not a design symbol")
    store.ensure_mutable()
    store.bind(pool["sha256"])
    if code_commit is not None:
        store.log(
            {
                "event": "collection_start",
                "code_commit": code_commit,
                "at": datetime.now(UTC).isoformat(),
                "symbols": selected,
                "refresh": refresh,
            }
        )
    allowed = set(contracts)
    tasks = [("BTCUSDT", "daily", BTC_START, END - DAY)]
    for symbol in selected:
        w = window(contracts[symbol])
        if w.empty:
            store.log({"symbol": symbol, "skip": "empty lifetime"})
            continue
        tasks.extend(
            [
                (symbol, "bars", w.start, w.end // HOUR * HOUR - HOUR),
                (
                    symbol,
                    "funding",
                    w.start - DAY,
                    min(END - 1, w.delist if w.delist is not None else END - 1),
                ),
            ]
        )
    for symbol, kind, first, last in tasks:
        # At most 200 hours of funding per page: supports even hourly settlement.
        step = DAY * 1000 if kind == "daily" else HOUR * (200 if kind == "funding" else 1000)
        cursor = first
        while cursor <= last:
            end = min(
                cursor + step - (1 if kind == "funding" else DAY if kind == "daily" else HOUR), last
            )
            key = encode([symbol, kind, cursor, end])
            saved = store.db.execute("SELECT payload FROM pages WHERE key=?", (key,)).fetchone()
            if saved is None or refresh:
                offset = len(client.attempts)
                try:
                    rows = client.page(symbol, kind, cursor, end, allowed)
                    with store.db:
                        store.put(kind, symbol, rows)
                        store.db.execute(
                            "INSERT OR REPLACE INTO pages VALUES (?,?)",
                            (key, encode({"rows": len(rows)})),
                        )
                        store.db.execute(
                            "INSERT INTO collection_log(content) VALUES (?)",
                            (
                                encode(
                                    {
                                        "page": json.loads(key),
                                        "refresh": refresh,
                                        "attempts": client.attempts[offset:],
                                        "error": None,
                                    }
                                ),
                            ),
                        )
                except BaseException as exc:
                    try:
                        store.log(
                            {
                                "page": json.loads(key),
                                "refresh": refresh,
                                "attempts": client.attempts[offset:],
                                "error": type(exc).__name__ + ": " + str(exc),
                            }
                        )
                    except Exception as log_error:
                        exc.add_note(f"collection audit also failed: {log_error}")
                    raise
                finally:
                    # Durable per-page audit owns these records; bound client memory.
                    client.attempts.clear()
                    client.requests.clear()
            cursor += step


def collect(
    pool: dict[str, Any],
    store: Store,
    client: DesignClient,
    *,
    symbols: list[str] | None = None,
    refresh: bool = False,
    code_commit: str | None = None,
) -> None:
    with dataset_lock(store.path):
        _collect(pool, store, client, symbols=symbols, refresh=refresh, code_commit=code_commit)
