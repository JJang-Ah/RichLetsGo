"""Daily S1-B channel breakout and deterministic D101 series views."""

from bisect import bisect_left
from decimal import Decimal

from richletsgo.research.d98.boundaries import DAY, HOUR
from richletsgo.research.d98.models import D, FundingPoint, RawBar, calculated
from richletsgo.research.d98.signals import slippage as d99_slippage

from .daily import daily_bars
from .models import Combination, liquidity_bucket


class Series:
    """Contract-local immutable market history and cached UTC daily bars."""

    def __init__(self, bars: list[RawBar], funding: list[FundingPoint]) -> None:
        if any(bar.start % HOUR for bar in bars):
            raise ValueError("hourly bar is off UTC grid")
        self.bars = {bar.start: bar for bar in bars}
        if len(self.bars) != len(bars):
            raise ValueError("duplicate hourly bar")
        self.times = sorted(self.bars)
        self.funding = sorted(funding, key=lambda point: point.time)
        self.funding_times = [point.time for point in self.funding]
        if len(set(self.funding_times)) != len(self.funding):
            raise ValueError("duplicate funding point")
        self.daily = daily_bars(bars)
        self.daily_times = sorted(self.daily)
        self._turnover_prefix = [D(0)]
        for at in self.times:
            self._turnover_prefix.append(self._turnover_prefix[-1] + self.bars[at].turnover)

    def complete(self, start: int, end: int) -> bool:
        return (
            bisect_left(self.times, end) - bisect_left(self.times, start) == (end - start) // HOUR
        )

    def daily_breakout(self, at: int, n: int) -> int:
        """Return +1/-1/0; B_0 is the day ending at ``at`` and is excluded from channel."""
        current = self.daily.get(at - DAY)
        previous = [self.daily.get(at - i * DAY) for i in range(2, n + 2)]
        if current is None or any(bar is None for bar in previous):
            return 0
        channel = [bar for bar in previous if bar is not None]
        if current.close > max(bar.high for bar in channel):
            return 1
        if current.close < min(bar.low for bar in channel):
            return -1
        return 0

    def entry_direction(self, at: int, combo: Combination) -> int:
        direction = self.daily_breakout(at, combo.n)
        return (
            1
            if combo.mode == "long" and direction == 1
            else direction
            if combo.mode == "both"
            else 0
        )

    @calculated
    def mean_dollar_turnover(self, at: int) -> Decimal:
        """Mean(D_1..D_30), matching 3.3's completed 24h windows before B_0."""
        totals = []
        for j in range(1, 31):
            day = self.daily.get(at - (j + 1) * DAY)
            if day is None:
                return D(0)
            totals.append(day.turnover)
        return sum(totals, D(0)) / 30

    def input_complete(self, at: int) -> bool:
        return self.complete(at - 56 * DAY, at)

    @calculated
    def features(self, at: int) -> tuple[dict[str, Decimal | str], str | None]:
        if not self.input_complete(at):
            return {}, "input_missing"
        mean = self.mean_dollar_turnover(at)
        if mean == 0:
            return {}, "zero_mean_turnover"
        slip = d99_slippage(mean)
        if slip is None:
            return {}, "liquidity"
        return {
            "mean_turnover": mean,
            "slippage": slip,
            "liquidity_bucket": liquidity_bucket(mean),
        }, None


def reversal(series: Series, at: int, direction: int, n: int) -> bool:
    return series.daily_breakout(at, n) == -direction
