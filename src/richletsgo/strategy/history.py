"""Explicit, immutable closed-bar boundary shared by research and live callers."""

from dataclasses import dataclass
from datetime import datetime

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
