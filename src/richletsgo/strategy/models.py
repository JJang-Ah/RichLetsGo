"""Immutable strategy parameters, persisted states and JSON-safe outputs."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from richletsgo.domain import Price, StrategyId, Symbol
from richletsgo.domain.clock import require_utc
from richletsgo.domain.values import finite_decimal
from richletsgo.marketdata.models import Interval
from richletsgo.portfolio.signals import MarketSignal
from richletsgo.risk.records import Direction

from .indicators import period, positive


class Trend(StrEnum):
    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, slots=True)
class S1AParams:
    fast: int
    slow: int
    exit_channel: int

    def __post_init__(self) -> None:
        for n in (self.fast, self.slow, self.exit_channel):
            period(n)
        if self.fast >= self.slow:
            raise ValueError("fast must be below slow")


@dataclass(frozen=True, slots=True)
class S1BParams:
    entry_channel: int
    exit_channel: int

    def __post_init__(self) -> None:
        period(self.entry_channel)
        period(self.exit_channel)


@dataclass(frozen=True, slots=True)
class S2AParams:
    period: int
    k: Decimal
    atr_period: int
    atr_multiple: Decimal
    max_hold_bars: int

    def __post_init__(self) -> None:
        for n in (self.period, self.atr_period, self.max_hold_bars):
            period(n)
        positive(self.k, "k")
        positive(self.atr_multiple, "atr_multiple")


@dataclass(frozen=True, slots=True)
class S2BParams:
    period: int
    low: Decimal
    high: Decimal
    neutral: Decimal
    atr_period: int
    atr_multiple: Decimal
    max_hold_bars: int

    def __post_init__(self) -> None:
        for n in (self.period, self.atr_period, self.max_hold_bars):
            period(n)
        for name in ("low", "high", "neutral"):
            finite_decimal(getattr(self, name), name)
        if not 0 < self.low < self.neutral < self.high < 100:
            raise ValueError("require 0 < low < neutral < high < 100")
        positive(self.atr_multiple, "atr_multiple")


@dataclass(frozen=True, slots=True)
class S1State:
    symbol: Symbol
    trend: Trend
    stop_price: Price | None
    bar_start: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, Symbol) or not isinstance(self.trend, Trend):
            raise TypeError("invalid state symbol/trend")
        if (self.trend is Trend.FLAT) != (self.stop_price is None):
            raise ValueError("only FLAT has no stop price")
        if self.stop_price is not None and not isinstance(self.stop_price, Price):
            raise TypeError("stop must be Price")
        if self.bar_start is not None and not Interval.DAY.aligned(self.bar_start):
            raise ValueError("S1 state must refer to an aligned daily bar")
        if self.trend is not Trend.FLAT and self.bar_start is None:
            raise ValueError("nonflat state requires its source bar")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol.value,
            "trend": self.trend.value,
            "stop_price": str(self.stop_price.value) if self.stop_price else None,
            "bar_start": self.bar_start.isoformat() if self.bar_start else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "S1State":
        return cls(
            Symbol(data["symbol"]),
            Trend(data["trend"]),
            Price(Decimal(data["stop_price"])) if data["stop_price"] is not None else None,
            datetime.fromisoformat(data["bar_start"]) if data["bar_start"] else None,
        )


@dataclass(frozen=True, slots=True)
class S2Episode:
    symbol: Symbol
    direction: Direction
    entry_bar_start: datetime
    stop_price: Price

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, Symbol) or not isinstance(self.direction, Direction):
            raise TypeError("invalid episode symbol/direction")
        if not Interval.HOUR.aligned(self.entry_bar_start):
            raise ValueError("entry bar must be hour aligned")
        if not isinstance(self.stop_price, Price):
            raise TypeError("episode stop must be Price")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol.value,
            "direction": self.direction.value,
            "entry_bar_start": self.entry_bar_start.isoformat(),
            "stop_price": str(self.stop_price.value),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "S2Episode":
        return cls(
            Symbol(data["symbol"]),
            Direction(data["direction"]),
            datetime.fromisoformat(data["entry_bar_start"]),
            Price(Decimal(data["stop_price"])),
        )


def signal_dict(signal: MarketSignal | None) -> dict[str, Any] | None:
    if signal is None:
        return None
    return {
        "strategy": signal.strategy.value,
        "symbol": signal.symbol.value,
        "bar_start": signal.bar_start.isoformat(),
        "kind": signal.kind,
        "direction": signal.direction.value,
        "entry_price": str(signal.entry_price.value),
        "stop_price": str(signal.stop_price.value),
        "limit_price": str(signal.limit_price.value) if signal.limit_price else None,
    }


def restore_signal(data: dict[str, Any] | None) -> MarketSignal | None:
    if data is None:
        return None
    return MarketSignal(
        StrategyId(data["strategy"]),
        Symbol(data["symbol"]),
        require_utc(datetime.fromisoformat(data["bar_start"])),
        data["kind"],
        Direction(data["direction"]),
        Price(Decimal(data["entry_price"])),
        Price(Decimal(data["stop_price"])),
        Price(Decimal(data["limit_price"])) if data["limit_price"] else None,
    )


@dataclass(frozen=True, slots=True)
class S1Result:
    state: S1State
    signal: MarketSignal | None

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state.to_dict(), "signal": signal_dict(self.signal)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "S1Result":
        return cls(S1State.from_dict(data["state"]), restore_signal(data["signal"]))


@dataclass(frozen=True, slots=True)
class S2Result:
    signal: MarketSignal | None

    def to_dict(self) -> dict[str, Any]:
        return {"signal": signal_dict(self.signal)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "S2Result":
        return cls(restore_signal(data["signal"]))
