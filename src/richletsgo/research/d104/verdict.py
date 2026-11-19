"""Pre-registered verdicts. Inputs are net of modeled execution costs and funding."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal as D
from statistics import stdev


@dataclass(frozen=True)
class Trade:
    day: date
    ended: int
    symbol: str
    pnl: D
    risk: D

    @property
    def net_r(self) -> D:
        if self.risk <= 0:
            raise ValueError("invalid initial risk")
        return self.pnl / self.risk


def minimum(slot_capacity: int) -> int:
    if slot_capacity < 0:
        raise ValueError("negative capacity")
    return max(100, (2 * slot_capacity + 4) // 5)


def design(
    trades: Sequence[Trade],
    capacity: int,
    first: date,
    until: date,
    *,
    slot_capacity: int,
    valid: bool,
    ruined: bool,
) -> str:
    if first >= until or any(not first <= t.day < until for t in trades):
        raise ValueError("INVALID_INPUT: trade date outside design range")
    if not valid or not 0 <= slot_capacity <= capacity:
        return "INVALID_INPUT"
    if capacity < 150:
        return "INDETERMINATE"
    if ruined or len(trades) < minimum(slot_capacity):
        return "NO_GO"
    daily = {first + timedelta(days=i): D(0) for i in range((until - first).days)}
    monthly = dict.fromkeys(((d.year, d.month) for d in daily), D(0))
    symbols: dict[str, D] = defaultdict(D)
    for t in trades:
        daily[t.day] += t.net_r
        monthly[t.day.year, t.day.month] += t.net_r
        symbols[t.symbol] += t.net_r
    values = list(daily.values())
    leaders = sorted(trades, key=lambda t: (-t.net_r, t.ended, t.symbol))
    remaining = leaders[(len(leaders) + 99) // 100 :]
    positive = [v for v in symbols.values() if v > 0]
    if len(values) < 2 or not positive:
        return "NO_GO"
    sd = stdev(values)
    passed = (
        sum(values) > 0
        and sd > 0
        and sum(values) / (D(len(values)).sqrt() * sd) >= 2
        and sum(t.net_r for t in remaining) > 0
        and sum(v > 0 for v in monthly.values()) * 2 > len(monthly)
        and max(positive) * 2 <= sum(positive)
    )
    return "GO" if passed else "NO_GO"


def holdout(trades: Sequence[Trade], ending: D, mdd: D, *, valid: bool, ruined: bool) -> str:
    if not valid:
        return "INVALID_INPUT"
    if ruined or ending <= 0 or mdd > D(".20"):
        return "FAIL"
    if len(trades) < 30 or len({t.symbol for t in trades}) < 4:
        return "INDETERMINATE"
    # Equivalent to annualized return >= 25%, without rounding display percentages.
    passed = (
        ending / D(10000) >= D("1.25") ** (D(71) / 365)
        and sum(t.net_r for t in trades) > 0
        and sum(t.pnl for t in trades) > max(t.pnl for t in trades)
    )
    return "PASS" if passed else "FAIL"
