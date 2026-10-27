"""Explicit, immutable closed-bar boundary shared by research and live callers."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from richletsgo.domain import Symbol
from richletsgo.domain.clock import require_utc
from richletsgo.marketdata.models import Bar, Interval


@dataclass(frozen=True, slots=True)
class ClosedBars:
    symbol: Symbol
    interval: Interval
    bars: tuple[Bar, ...]
    as_of: datetime

    def __post_init__(self) -> None:
        require_utc(self.as_of)
        if not isinstance(self.symbol, Symbol) or not isinstance(self.interval, Interval):
            raise TypeError("history requires Symbol and Interval")
        if not isinstance(self.bars, tuple):
            raise TypeError("bars must be an immutable tuple")
        previous = None
        for bar in self.bars:
            if bar.symbol is not self.symbol or bar.interval is not self.interval:
                raise ValueError("mixed symbol or interval")
            if bar.end > self.as_of:
                raise ValueError("unfinished bar")
            if previous is not None and bar.start != previous.end:
                raise ValueError("bars must be ordered, unique and contiguous")
            previous = bar

    def through(self, start: datetime, *, lookback: int | None = None) -> "ClosedBars":
        """Select before calculation; bounded lookback avoids growing-prefix work."""
        require_utc(start)
        if lookback is not None and (type(lookback) is not int or lookback < 1):
            raise ValueError("lookback must be a positive integer")
        # Contiguity was validated at construction, so no linear search is needed.
        end = (
            min(len(self.bars), max(0, (start - self.bars[0].start) // self.interval.duration + 1))
            if self.bars
            else 0
        )
        begin = max(0, end - lookback) if lookback is not None else 0
        return ClosedBars(self.symbol, self.interval, self.bars[begin:end], self.as_of)

    def before_last(self, *, lookback: int) -> "ClosedBars":
        if type(lookback) is not int or lookback < 1:
            raise ValueError("lookback must be a positive integer")
        return ClosedBars(self.symbol, self.interval, self.bars[-lookback - 1 : -1], self.as_of)


def four_hour_bars(bars: tuple[Bar, ...], *, as_of: datetime) -> tuple[Bar, ...]:
    """Aggregate complete UTC groups only; never fill gaps or use unfinished input."""
    require_utc(as_of)
    groups: dict[datetime, list[Bar]] = {}
    symbol = bars[0].symbol if bars else None
    previous = None
    for bar in bars:
        if bar.interval is not Interval.HOUR or bar.symbol is not symbol:
            raise ValueError("matching hourly bars required")
        if previous is not None and bar.start <= previous:
            raise ValueError("ordered unique bars required")
        if bar.end > as_of:
            raise ValueError("unfinished hourly bar")
        previous = bar.start
        groups.setdefault(Interval.FOUR_HOUR.floor(bar.start), []).append(bar)
    result = []
    for start, group in groups.items():
        if len(group) != 4 or any(
            b.start != start + i * Interval.HOUR.duration for i, b in enumerate(group)
        ):
            continue
        result.append(
            Bar(
                group[0].symbol,
                Interval.FOUR_HOUR,
                start,
                group[0].open,
                max(b.high for b in group),
                min(b.low for b in group),
                group[-1].close,
                sum((b.volume for b in group), Decimal(0)),
                sum((b.turnover for b in group), Decimal(0)),
            )
        )
    return tuple(result)
