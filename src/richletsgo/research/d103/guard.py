"""Request guard: allowlisted symbols, explicit time bounds below B, no tickers (M2-10 2-3)."""

from typing import Any
from urllib.parse import parse_qsl, urlsplit

from richletsgo.marketdata.bybit_public import BybitPublicClient, Transport

from .models import (
    BOOK_KEYS,
    BOOK_LIMITS,
    BOUNDARY,
    HISTORY_INTERVALS,
    HOST,
    HOUR,
    PRICE_KLINE_PATHS,
    TYPES,
)

FUNDING_WINDOW = 200 * HOUR  # <=200 settlements per page even at 1h intervals


class ObservationIncident(Exception):
    """The API returned data at or after B; nothing from the response is stored."""


class StructureTransport:
    def __init__(self, inner: Transport, launches: dict[str, int]) -> None:
        self.inner = inner
        self.launches = dict(launches)

    def _symbol(self, p: dict[str, str]) -> str:
        symbol = p.get("symbol")
        if symbol is None or symbol not in self.launches:
            raise ValueError("symbol must be explicit and allowlisted")
        return symbol

    def _history(self, p: dict[str, str], start_key: str, end_key: str, last: int) -> None:
        symbol = self._symbol(p)
        if self.launches[symbol] >= BOUNDARY:
            raise ValueError("no historical request for symbols launched at or after B")
        if start_key not in p or end_key not in p:
            raise ValueError("historical request needs both time bounds")
        start, end = int(p[start_key]), int(p[end_key])
        if start > end or end > last:
            raise ValueError("historical request reaches the reserved range")

    def get(self, url: str, timeout: float) -> tuple[int, bytes]:
        u = urlsplit(url)
        pairs = parse_qsl(u.query, keep_blank_values=True)
        p = dict(pairs)
        if u.scheme != "https" or u.netloc != HOST or u.fragment or len(p) != len(pairs):
            raise ValueError("unsupported endpoint")
        if p.get("category") != "linear":
            raise ValueError("linear category only")
        if u.path == "/v5/market/instruments-info":
            if set(p) != {"category", "symbolType", "limit"} or p["symbolType"] not in TYPES:
                raise ValueError("metadata change detection only")
        elif u.path == "/v5/market/kline" or u.path in PRICE_KLINE_PATHS:
            interval = p.get("interval", "")
            allowed = {"60", "5"} if u.path == "/v5/market/kline" else {"60"}
            if (
                set(p) != {"category", "symbol", "interval", "start", "end", "limit"}
                or interval not in allowed
                or p["limit"] != "1000"
            ):
                raise ValueError("unsupported kline request")
            # Last bar start must leave the whole bar before B.
            self._history(p, "start", "end", BOUNDARY - HISTORY_INTERVALS[interval])
        elif u.path == "/v5/market/funding/history":
            if set(p) != {"category", "symbol", "startTime", "endTime", "limit"} or (
                p["limit"] != "200"
            ):
                raise ValueError("unsupported funding request")
            self._history(p, "startTime", "endTime", BOUNDARY - 1)
        elif u.path == "/v5/market/orderbook":
            self._symbol(p)
            if set(p) != {"category", "symbol", "limit"} or p["limit"] not in BOOK_LIMITS:
                raise ValueError("unsupported orderbook request")
        else:
            # Includes /v5/market/tickers: rolling 24h fields would reveal [B, T0).
            raise ValueError("endpoint forbidden")
        return self.inner.get(url, timeout)


class StructureClient(BybitPublicClient):
    def instruments(self, symbol_type: str) -> dict[str, Any]:
        return self._get(
            "/v5/market/instruments-info",
            {"category": "linear", "symbolType": symbol_type, "limit": "1000"},
        )

    def bars(self, path: str, symbol: str, interval: str, start: int, end: int) -> list[Any]:
        result = self._get(
            path,
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "start": str(start),
                "end": str(end),
                "limit": "1000",
            },
        )
        if result.get("symbol", symbol) != symbol:
            raise ValueError("response identity mismatch")
        rows = result.get("list")
        if not isinstance(rows, list):
            raise ValueError("missing response rows")
        width = HISTORY_INTERVALS[interval]
        if any(int(r[0]) + width > BOUNDARY for r in rows):
            raise ObservationIncident(f"{path} {symbol} returned a bar reaching B")
        return rows

    def funding(self, symbol: str, start: int, end: int) -> list[list[str]]:
        result = self._get(
            "/v5/market/funding/history",
            {
                "category": "linear",
                "symbol": symbol,
                "startTime": str(start),
                "endTime": str(end),
                "limit": "200",
            },
        )
        rows = result.get("list")
        if not isinstance(rows, list) or any(r.get("symbol") != symbol for r in rows):
            raise ValueError("funding response identity mismatch")
        if any(int(r["fundingRateTimestamp"]) >= BOUNDARY for r in rows):
            raise ObservationIncident(f"funding {symbol} returned a settlement at or after B")
        if len(rows) >= 200:
            raise ValueError("funding page may be truncated; window too wide")
        return [[r["fundingRateTimestamp"], r["fundingRate"]] for r in rows]

    def orderbook(self, symbol: str, limit: str) -> dict[str, Any]:
        result = self._get(
            "/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": limit}
        )
        if not set(result) <= BOOK_KEYS:
            raise ValueError(f"unexpected orderbook fields: {sorted(set(result) - BOOK_KEYS)}")
        if result.get("s") != symbol:
            raise ValueError("orderbook identity mismatch")
        return result
