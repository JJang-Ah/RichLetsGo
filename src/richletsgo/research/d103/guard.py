"""Request guard: allowlisted symbols, explicit time bounds below B, no tickers (M2-10 2-3)."""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from richletsgo.marketdata.bybit_public import BybitApiError, BybitPublicClient, Transport

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

# [start, start + 199h - 1ms] holds at most 199 settlements even at 1h intervals, so a
# 200-row page always means truncation (PR #61 P3).
FUNDING_WINDOW = 199 * HOUR


def _now_ms() -> int:
    return int(time.time() * 1000)


class ObservationIncident(Exception):
    """The API returned data outside the observed range; nothing from it is stored.

    `details` keeps the symbol, request and observed time range (never prices) so the
    exposed part of [B, T0) can be excluded from D104 candidates (PR #61 P2-1).
    """

    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.details = {"detected_at": datetime.now(UTC).isoformat(), **details}


class EmptyResponse(BybitApiError):
    """A historical page stayed empty after every retry; logged missing, never done."""


class LiveWindowClosed(Exception):
    """An orderbook request was about to start at or after T0 + 14 days."""


class StructureTransport:
    def __init__(
        self,
        inner: Transport,
        launches: dict[str, int],
        *,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        self.inner = inner
        self.launches = dict(launches)
        self.clock = clock
        self.book_until: int | None = None  # set by live collection: [T0, T0 + 14d)

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
            # Checked before every attempt, retries included (PR #61 P2-3).
            if self.book_until is None:
                raise ValueError("orderbook requests need an open live window")
            if self.clock() >= self.book_until:
                raise LiveWindowClosed("live window closed")
        else:
            # Includes /v5/market/tickers: rolling 24h fields would reveal [B, T0).
            raise ValueError("endpoint forbidden")
        return self.inner.get(url, timeout)


class StructureClient(BybitPublicClient):
    @property
    def guard(self) -> StructureTransport:
        if not isinstance(self._transport, StructureTransport):
            raise TypeError("StructureClient needs a StructureTransport")
        return self._transport

    def instruments(self, symbol_type: str) -> dict[str, Any]:
        return self._get(
            "/v5/market/instruments-info",
            {"category": "linear", "symbolType": symbol_type, "limit": "1000"},
        )

    def _listed(self, path: str, params: dict[str, str]) -> tuple[dict[str, Any], list[Any]]:
        """An empty raw list is retried like an error; still empty is missing (M2-10 3)."""
        for attempt in range(self._policy.max_attempts):
            if attempt:
                self._sleep(self._policy.backoff_seconds * 2 ** (attempt - 1))
            result = self._get(path, params)
            rows = result.get("list")
            if not isinstance(rows, list):
                raise ValueError("missing response rows")
            if rows:
                return result, rows
        raise EmptyResponse(f"{path} stayed empty after {self._policy.max_attempts} attempts")

    def bars(self, path: str, symbol: str, interval: str, start: int, end: int) -> list[Any]:
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "start": str(start),
            "end": str(end),
            "limit": "1000",
        }
        result, rows = self._listed(path, params)
        if result.get("symbol", symbol) != symbol:
            raise ValueError("response identity mismatch")
        width = HISTORY_INTERVALS[interval]
        starts = [int(r[0]) for r in rows]
        if any(t + width > BOUNDARY for t in starts):
            raise ObservationIncident(
                f"{path} {symbol} returned a bar reaching B",
                {
                    "symbol": symbol,
                    "path": path,
                    "params": params,
                    "observed_first": min(starts),
                    "observed_last_end": max(starts) + width,
                },
            )
        return rows

    def funding(self, symbol: str, start: int, end: int) -> list[list[str]]:
        path = "/v5/market/funding/history"
        params = {
            "category": "linear",
            "symbol": symbol,
            "startTime": str(start),
            "endTime": str(end),
            "limit": "200",
        }
        _, rows = self._listed(path, params)
        if any(r.get("symbol") != symbol for r in rows):
            raise ValueError("funding response identity mismatch")
        times = [int(r["fundingRateTimestamp"]) for r in rows]
        if any(t >= BOUNDARY for t in times):
            raise ObservationIncident(
                f"funding {symbol} returned a settlement at or after B",
                {
                    "symbol": symbol,
                    "path": path,
                    "params": params,
                    "observed_first": min(times),
                    "observed_last": max(times),
                },
            )
        if len(rows) >= 200:
            raise ValueError("funding page may be truncated; window too wide")
        return [[r["fundingRateTimestamp"], r["fundingRate"]] for r in rows]

    def orderbook(self, symbol: str, limit: str) -> dict[str, Any]:
        requested = self.guard.clock()
        result = self._get(
            "/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": limit}
        )
        if not set(result) <= BOOK_KEYS:
            raise ObservationIncident(
                f"unexpected orderbook fields for {symbol}",
                {
                    "symbol": symbol,
                    "path": "/v5/market/orderbook",
                    "params": {"limit": limit},
                    "fields": sorted(set(result) - BOOK_KEYS),
                    "requested": requested,
                    "ts": result.get("ts"),
                },
            )
        if result.get("s") != symbol:
            raise ValueError("orderbook identity mismatch")
        return result
