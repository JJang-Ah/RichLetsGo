"""Read-only Bybit V5 public market client (no API key). Transport is injected for tests."""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from richletsgo.domain import Symbol

from .models import InstrumentSnapshot, Interval, RiskLimitTier, from_ms, to_ms

KLINE_PAGE = 1000  # Bybit maximum per kline request
FUNDING_PAGE = 200  # Bybit maximum per funding-history request


class TransportError(Exception):
    """Network-level failure (timeout, DNS, connection reset); retryable."""


class Transport(Protocol):
    def get(self, url: str, timeout: float) -> tuple[int, bytes]: ...


class UrllibTransport:
    def get(self, url: str, timeout: float) -> tuple[int, bytes]:
        request = urllib.request.Request(url, headers={"User-Agent": "richletsgo-marketdata"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise TransportError(str(exc)) from exc


class BybitApiError(Exception):
    """Request failed after retries, or failed with a non-retryable status."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Request pacing — operational settings, not risk values (PR #12 I12)."""

    pause_seconds: float
    max_attempts: int
    backoff_seconds: float
    timeout_seconds: float

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.pause_seconds < 0 or self.backoff_seconds < 0:
            raise ValueError("invalid retry policy")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout must be positive")


class BybitPublicClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport,
        policy: RetryPolicy,
        sleep: Callable[[float], None],
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("base_url must be an https URL")
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._policy = policy
        self._sleep = sleep
        self.attempts: list[dict[str, Any]] = []
        self.requests: list[str] = []  # observed request URLs (tests and run summary)

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self._base}{path}?{urllib.parse.urlencode(params)}"
        last_error = ""
        for attempt in range(1, self._policy.max_attempts + 1):
            if attempt > 1:
                self._sleep(self._policy.backoff_seconds * 2 ** (attempt - 2))
            self.requests.append(url)
            self.attempts.append(
                {"url": url, "attempt": attempt, "at": datetime.now(UTC).isoformat()}
            )
            try:
                status, body = self._transport.get(url, self._policy.timeout_seconds)
            except TransportError as exc:
                last_error = f"transport: {exc}"
                continue
            finally:
                self._sleep(self._policy.pause_seconds)
            if status == 429 or status >= 500:
                last_error = f"http {status}"
                continue
            if status != 200:
                raise BybitApiError(f"http {status} for {path}")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                last_error = "invalid json"
                continue
            if payload.get("retCode") != 0:
                last_error = f"retCode {payload.get('retCode')}: {payload.get('retMsg')}"
                continue
            result = payload.get("result")
            if not isinstance(result, dict):
                raise BybitApiError(f"missing result for {path}")
            return result
        raise BybitApiError(
            f"{path} failed after {self._policy.max_attempts} attempts: {last_error}"
        )

    def klines(
        self, symbol: Symbol, interval: Interval, first: datetime, last: datetime
    ) -> list[list[str]]:
        """Raw rows for bar starts in [first, last], fetched oldest page first."""
        rows: list[list[str]] = []
        cursor = first
        step = interval.duration * (KLINE_PAGE - 1)
        while cursor <= last:
            page_end = min(cursor + step, last)
            result = self._get(
                "/v5/market/kline",
                {
                    "category": "linear",
                    "symbol": symbol.value,
                    "interval": interval.value,
                    "start": str(to_ms(cursor)),
                    "end": str(to_ms(page_end)),
                    "limit": str(KLINE_PAGE),
                },
            )
            rows.extend(result.get("list", []))
            cursor = page_end + interval.duration
        return rows

    def funding_history(
        self, symbol: Symbol, first: datetime, last: datetime
    ) -> list[dict[str, str]]:
        """Page backwards with endTime only (Bybit rejects startTime alone)."""
        rows: list[dict[str, str]] = []
        end = last
        while end >= first:
            result = self._get(
                "/v5/market/funding/history",
                {
                    "category": "linear",
                    "symbol": symbol.value,
                    "endTime": str(to_ms(end)),
                    "limit": str(FUNDING_PAGE),
                },
            )
            page = result.get("list", [])
            if not page:
                break
            rows.extend(page)
            oldest = min(int(r["fundingRateTimestamp"]) for r in page)
            if from_ms(oldest) <= first or len(page) < FUNDING_PAGE:
                break
            end = from_ms(oldest) - timedelta(milliseconds=1)
        return [r for r in rows if first <= from_ms(int(r["fundingRateTimestamp"])) <= last]

    def instrument(self, symbol: Symbol, captured_at: datetime) -> InstrumentSnapshot:
        result = self._get(
            "/v5/market/instruments-info", {"category": "linear", "symbol": symbol.value}
        )
        items = [i for i in result.get("list", []) if i.get("symbol") == symbol.value]
        if len(items) != 1:
            raise BybitApiError(f"instrument {symbol} not found exactly once")
        item = items[0]
        return InstrumentSnapshot(
            symbol,
            Decimal(item["lotSizeFilter"]["qtyStep"]),
            Decimal(item["lotSizeFilter"]["minOrderQty"]),
            Decimal(item["priceFilter"]["tickSize"]),
            Decimal(item["leverageFilter"]["maxLeverage"]),
            timedelta(minutes=int(item["fundingInterval"])),
            captured_at,
        )

    def risk_limit_tiers(self, symbol: Symbol, captured_at: datetime) -> list[RiskLimitTier]:
        """All tiers via cursor paging. Linear margins are ratios (PR #12 I7, verified)."""
        items: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(100):  # hard stop against a cursor loop
            params = {"category": "linear", "symbol": symbol.value}
            if cursor:
                params["cursor"] = cursor
            result = self._get("/v5/market/risk-limit", params)
            items.extend(i for i in result.get("list", []) if i.get("symbol") == symbol.value)
            cursor = str(result.get("nextPageCursor") or "")
            if not cursor:
                break
        else:
            raise BybitApiError("risk-limit pagination did not finish")
        items.sort(key=lambda i: Decimal(i["riskLimitValue"]))
        return [
            RiskLimitTier(
                symbol,
                index,
                Decimal(i["riskLimitValue"]),
                Decimal(i["maintenanceMargin"]),
                Decimal(i["initialMargin"]),
                Decimal(i["maxLeverage"]),
                captured_at,
            )
            for index, i in enumerate(items, start=1)
        ]
