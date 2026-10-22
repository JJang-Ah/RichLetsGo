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

    def through(self, start: datetime) -> "ClosedBars":
        """Select a historical prefix before computing any indicator or signal."""
        require_utc(start)
        return ClosedBars(
            self.symbol, self.interval, tuple(b for b in self.bars if b.start <= start), self.as_of
        )

    def before_last(self) -> "ClosedBars":
        return ClosedBars(self.symbol, self.interval, self.bars[:-1], self.as_of)
