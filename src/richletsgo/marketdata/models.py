"""Historical market data records for backtests (M2-01). Values are Decimal, times UTC."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from richletsgo.domain import InstrumentSpec, Symbol
from richletsgo.domain.clock import require_utc
from richletsgo.domain.values import finite_decimal

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def from_ms(ms: int) -> datetime:
    """Exact conversion of a Bybit millisecond timestamp to an aware UTC datetime."""
    if type(ms) is not int:
        raise TypeError("timestamp must be int milliseconds")
    return EPOCH + timedelta(milliseconds=ms)


def to_ms(ts: datetime) -> int:
    return (require_utc(ts) - EPOCH) // timedelta(milliseconds=1)


class Interval(StrEnum):
    """Bybit kline interval codes used by the backtest (D67, D70, 04 3.1)."""

    DAY = "D"
    HOUR = "60"
    FOUR_HOUR = "240"
    MINUTE = "1"

    @property
    def duration(self) -> timedelta:
        return {
            Interval.DAY: timedelta(days=1),
            Interval.HOUR: timedelta(hours=1),
            Interval.FOUR_HOUR: timedelta(hours=4),
            Interval.MINUTE: timedelta(minutes=1),
        }[self]

    def aligned(self, ts: datetime) -> bool:
        return (require_utc(ts) - EPOCH) % self.duration == timedelta(0)

    def floor(self, ts: datetime) -> datetime:
        return EPOCH + ((require_utc(ts) - EPOCH) // self.duration) * self.duration


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: Symbol
    interval: Interval
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, Symbol) or not isinstance(self.interval, Interval):
            raise TypeError("bar requires Symbol and Interval")
        if not self.interval.aligned(self.start):
            raise ValueError("bar start is not aligned to its interval")
        for name in ("open", "high", "low", "close"):
            if finite_decimal(getattr(self, name), name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("volume", "turnover"):
            if finite_decimal(getattr(self, name), name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC relationship is violated")

    @property
    def end(self) -> datetime:
        return self.start + self.interval.duration

    def same_values(self, other: "Bar") -> bool:
        """Decimal value equality; textual scale differences (0.1 vs 0.10) are not conflicts."""
        fields = ("open", "high", "low", "close", "volume", "turnover")
        return all(getattr(self, f) == getattr(other, f) for f in fields)


@dataclass(frozen=True, slots=True)
class FundingRate:
    symbol: Symbol
    funding_time: datetime
    rate: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, Symbol):
            raise TypeError("symbol must be Symbol")
        require_utc(self.funding_time)
        finite_decimal(self.rate, "rate")


@dataclass(frozen=True, slots=True)
class InstrumentSnapshot:
    symbol: Symbol
    qty_step: Decimal
    min_qty: Decimal
    tick_size: Decimal
    max_leverage: Decimal
    funding_interval: timedelta
    captured_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.captured_at)
        if self.funding_interval <= timedelta(0):
            raise ValueError("funding_interval must be positive")
        self.to_spec()  # reuse InstrumentSpec validation

    def to_spec(self) -> InstrumentSpec:
        return InstrumentSpec(
            self.symbol, self.qty_step, self.min_qty, self.tick_size, self.max_leverage
        )


IMR_LEVERAGE_TOLERANCE = Decimal("0.02")  # unit sanity check only, not a risk value


@dataclass(frozen=True, slots=True)
class RiskLimitTier:
    """Bybit risk-limit tier captured at `captured_at` — not a historical value (D79)."""

    symbol: Symbol
    tier: int
    limit_notional: Decimal
    mmr: Decimal
    imr: Decimal
    max_leverage: Decimal
    captured_at: datetime

    def __post_init__(self) -> None:
        if type(self.tier) is not int or self.tier < 1:
            raise ValueError("tier must be a positive int")
        require_utc(self.captured_at)
        if finite_decimal(self.limit_notional, "limit_notional") <= 0:
            raise ValueError("limit_notional must be positive")
        # The highest Bybit tier is 1x with an initial margin of exactly 1 (100%).
        if not 0 < finite_decimal(self.imr, "imr") <= 1:
            raise ValueError("imr must be a ratio within (0, 1]")
        if not 0 < finite_decimal(self.mmr, "mmr") < self.imr:
            raise ValueError("mmr must be positive and below imr")
        if finite_decimal(self.max_leverage, "max_leverage") < 1:
            raise ValueError("max_leverage must be at least 1")
        # Unit check (PR #12 I7): linear margins are ratios, so imr * max_leverage ~= 1.
        if (self.imr * self.max_leverage - 1).copy_abs() > IMR_LEVERAGE_TOLERANCE:
            raise ValueError("initial margin does not match max leverage; unexpected units")
