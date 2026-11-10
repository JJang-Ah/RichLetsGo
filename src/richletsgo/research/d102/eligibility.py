"""Contract-local daily eligibility; no future data is used for selection."""

from bisect import bisect_left
from decimal import Decimal
from typing import Any

from richletsgo.research.d98.boundaries import DAY, HOUR
from richletsgo.research.d98.models import D, FundingPoint, RawBar, calculated
from richletsgo.research.d98.signals import slippage

from .funding import FundingSeries


class Series:
    @calculated
    def __init__(self, bars: list[RawBar], funding: list[FundingPoint]) -> None:
        self.bars = {b.start: b for b in bars}
        if len(self.bars) != len(bars) or any(b.start % HOUR for b in bars):
            raise ValueError("duplicate or off-grid hourly bars")
        self.times = sorted(self.bars)
        self.prefix = [D(0)]
        for at in self.times:
            self.prefix.append(self.prefix[-1] + self.bars[at].turnover)
        self.funding = FundingSeries(funding)

    def complete(self, begin: int, end: int) -> bool:
        return (
            bisect_left(self.times, end) - bisect_left(self.times, begin) == (end - begin) // HOUR
        )

    @calculated
    def turnover_mean(self, at: int) -> Decimal:
        return (
            self.prefix[bisect_left(self.times, at - DAY)]
            - self.prefix[bisect_left(self.times, at - 31 * DAY)]
        ) / 30

    @calculated
    def classify(self, at: int, launch: int) -> tuple[str, dict[str, Any]]:
        if at - launch < 60 * DAY:
            return "metadata_ineligible", {}
        if not self.complete(at - 31 * DAY, at) or not self.funding.has_input(at):
            return "input_insufficient", {}
        mean = self.turnover_mean(at)
        if mean == 0:
            return "zero_mean", {}
        if mean < D(20_000_000):
            return "liquidity", {}
        one, error1 = self.funding.score(at, 1)
        seven, error7 = self.funding.score(at, 7)
        if error1 or error7:
            return "funding_coverage", {}
        return "eligible", {
            "score1": one,
            "score7": seven,
            "slippage": slippage(mean),
            "mean_turnover": mean,
        }
