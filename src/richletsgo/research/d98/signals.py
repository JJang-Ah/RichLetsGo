"""Finite-window indicators and D98 S2-M signal rules, no network or DB."""

from bisect import bisect_left, bisect_right
from decimal import Decimal
from statistics import median

from .boundaries import DAY, HOUR
from .models import FUNDING_LIMIT, D, FundingPoint, RawBar, calculated


@calculated
def four_hour_bars(bars: list[RawBar]) -> dict[int, RawBar]:
    source = {b.start: b for b in bars}
    if len(source) != len(bars):
        raise ValueError("duplicate hourly bar")
    result = {}
    for start in sorted(source):
        if start % (4 * HOUR):
            continue
        group = [source.get(start + i * HOUR) for i in range(4)]
        if any(b is None for b in group):
            continue
        complete = [b for b in group if b is not None]
        result[start + 4 * HOUR] = RawBar(
            start,
            complete[0].open,
            max(b.high for b in complete),
            min(b.low for b in complete),
            complete[-1].close,
            sum((b.volume for b in complete), D(0)),
            sum((b.turnover for b in complete), D(0)),
        )
    return result


@calculated
def ema(bars: list[RawBar], n: int = 20) -> Decimal:
    if len(bars) != 10 * n:
        raise ValueError("EMA requires fixed 10n window")
    value = sum((b.close for b in bars[:n]), D(0)) / n
    alpha = D(2) / (n + 1)
    for b in bars[n:]:
        value = alpha * b.close + (1 - alpha) * value
    return value


@calculated
def atr(bars: list[RawBar], n: int = 14) -> Decimal:
    if len(bars) != 10 * n:
        raise ValueError("ATR requires fixed 10n window")
    ranges = []
    previous = None
    for b in bars:
        tr = b.high - b.low
        if previous is not None:
            tr = max(tr, abs(b.high - previous.close), abs(b.low - previous.close))
        ranges.append(tr)
        previous = b
    value = sum(ranges[:n], D(0)) / n
    for tr in ranges[n:]:
        value = (value * (n - 1) + tr) / n
    return value


@calculated
def funding_state(points: list[FundingPoint], t: int) -> tuple[Decimal | None, str | None]:
    if len(points) != 3:
        return None, "funding_missing"
    a, b, c = points
    interval = c.time - b.time
    if (
        c.time > t
        or interval != b.time - a.time
        or interval not in {HOUR, 2 * HOUR, 4 * HOUR, 8 * HOUR}
    ):
        return None, "funding_interval"
    if t - c.time > interval:
        return None, "funding_stale"
    return c.rate * (8 * HOUR) / interval, None


def slippage(mean: Decimal) -> Decimal | None:
    for threshold, value in (
        (200_000_000, "0.0005"),
        (50_000_000, "0.0010"),
        (20_000_000, "0.0015"),
        (5_000_000, "0.0025"),
    ):
        if mean >= threshold:
            return D(value)
    return None


class Series:
    @calculated
    def __init__(self, bars: list[RawBar], funding: list[FundingPoint]) -> None:
        if any(b.start % HOUR for b in bars):
            raise ValueError("off-grid bar")
        self.bars = {b.start: b for b in bars}
        self.times = sorted(self.bars)
        self.funding = sorted(funding, key=lambda p: p.time)
        self.funding_times = [p.time for p in self.funding]
        if len(self.bars) != len(bars) or len(set(self.funding_times)) != len(funding):
            raise ValueError("duplicate data")
        self.four = four_hour_bars(bars)
        self.prefix = [D(0)]
        for t in self.times:
            self.prefix.append(self.prefix[-1] + self.bars[t].turnover)
        self.atr_cache: dict[int, Decimal] = {}
        self.ema_cache: dict[int, Decimal] = {}

    def complete(self, start: int, end: int) -> bool:
        return (
            bisect_left(self.times, end) - bisect_left(self.times, start) == (end - start) // HOUR
        )

    def past_funding(self, t: int) -> list[FundingPoint]:
        end = bisect_right(self.funding_times, t)
        return self.funding[max(0, end - 3) : end]

    def input_complete(self, t: int) -> bool:
        return self.complete(t - 800 * HOUR, t) and len(self.past_funding(t)) == 3

    def four_window(self, t: int, n: int) -> list[RawBar]:
        return [self.four[u] for u in range(t - (n - 1) * 4 * HOUR, t + 1, 4 * HOUR)]

    def atr_at(self, t: int) -> Decimal:
        if t not in self.atr_cache:
            self.atr_cache[t] = atr(self.four_window(t, 140))
        return self.atr_cache[t]

    def ema_at(self, t: int) -> Decimal:
        if t not in self.ema_cache:
            self.ema_cache[t] = ema(self.four_window(t, 200))
        return self.ema_cache[t]

    @calculated
    def turnover(self, start: int, end: int) -> Decimal:
        return (
            self.prefix[bisect_left(self.times, end)] - self.prefix[bisect_left(self.times, start)]
        )

    @calculated
    def features(self, t: int) -> tuple[dict[str, Decimal], str | None]:
        if not self.input_complete(t):
            return {}, "input_missing"
        daily = [self.turnover(t - (j + 1) * DAY, t - j * DAY) for j in range(1, 31)]
        middle = median(daily)
        if middle == 0:
            return {}, "zero_median"
        mean = sum(daily, D(0)) / 30
        slip = slippage(mean)
        if slip is None:
            return {}, "liquidity"
        rate, reason = funding_state(self.past_funding(t), t)
        if reason:
            return {}, reason
        value = self.atr_at(t)
        if value == 0:
            return {}, "zero_atr"
        assert rate is not None
        return {
            "V": self.turnover(t - DAY, t) / middle,
            "mean_turnover": mean,
            "slippage": slip,
            "atr": value,
            "funding_8h": rate,
        }, None

    @calculated
    def signal(self, t: int, direction: int, v: int, n: int, features: dict[str, Decimal]) -> bool:
        if direction not in (-1, 1) or features["V"] < v:
            return False
        current = self.four[t].close
        previous = [self.four[u] for u in range(t - n * 4 * HOUR, t, 4 * HOUR)]
        if direction == 1 and current <= max(b.high for b in previous):
            return False
        if direction == -1 and current >= min(b.low for b in previous):
            return False
        if abs(current - self.ema_at(t)) > 3 * features["atr"]:
            return False
        return direction * features["funding_8h"] < FUNDING_LIMIT
