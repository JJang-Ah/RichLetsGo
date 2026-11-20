"""Offline design verdict and the complete registered diagnostics."""

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from statistics import stdev
from typing import Any

from .inputs import CALENDAR, DAY, B
from .replay import Fixture, replay
from .verdict import design


def report(fixtures: list[Fixture]) -> dict[str, Any]:
    result = replay(fixtures)
    first_ms = min(((f.launch + DAY - 1) // DAY + 30) * DAY for f in fixtures)
    first = datetime.fromtimestamp(first_ms // 1000, UTC).date()
    until = datetime.fromtimestamp(B // 1000, UTC).date()
    trades = result.trades
    verdict = design(
        trades, 491, first, until, slot_capacity=232, valid=True, ruined=result.minimum_equity <= 0
    )
    daily = {first + timedelta(days=i): D(0) for i in range((until - first).days)}
    monthly = dict.fromkeys((d.isoformat()[:7] for d in daily), D(0))
    monthly_pnl = monthly.copy()
    sym_r = dict.fromkeys(CALENDAR, D(0))
    pnl = sym_r.copy()
    for t in trades:
        daily[t.day] += t.net_r
        month = t.day.isoformat()[:7]
        monthly[month] += t.net_r
        monthly_pnl[month] += t.pnl
        sym_r[t.symbol] += t.net_r
        pnl[t.symbol] += t.pnl
    positive = [v for v in sym_r.values() if v > 0]
    n = len(trades)
    leaders = sorted(trades, key=lambda t: (-t.net_r, t.ended, t.symbol))
    remains = leaders[(n + 99) // 100 :]
    sd = stdev(daily.values()) if len(daily) > 1 else D(0)
    costs = {}
    for sym in ["ALL", *sorted(CALENDAR)]:
        rows = [(s, f, c, r) for k, s, f, c, r in result.costs if sym == "ALL" or sym == k]
        ratios = sorted((s + f) / r for s, f, c, r in rows)
        costs[sym] = dict(
            slippage=sum(r[0] for r in rows),
            fees=sum(r[1] for r in rows),
            funding_cash=sum(r[2] for r in rows),
            at_least_one=sum(v >= 1 for v in ratios) / D(len(rows)) if rows else None,
            quantiles={
                str(p): ratios[(len(rows) * p + 99) // 100 - 1] if rows else None
                for p in (50, 90, 95)
            },
        )
    rejected: dict[str, dict[str, int]] = defaultdict(dict)
    tiers: Counter[str] = Counter()
    for (sym, slip), count in result.cost_rejections.items():
        rejected[sym][str(slip)] = count
        tiers[str(slip)] += count
    return dict(
        verdict=verdict,
        first_utc=str(first),
        until_exclusive=str(until),
        E=491,
        E_slot=232,
        N_min=100,
        N=n,
        equity=result.equity,
        net_pnl=result.equity - 10000,
        minimum_equity=result.minimum_equity,
        mdd=result.mdd,
        mdd_at=result.mdd_at,
        digest=result.digest,
        average_net_r=sum(daily.values()) / n if n else None,
        daily_t=sum(daily.values()) / (D(len(daily)).sqrt() * sd) if sd else None,
        trimmed_mean_r=sum(t.net_r for t in remains) / len(remains) if remains else None,
        monthly_r=monthly,
        monthly_pnl=monthly_pnl,
        positive_months=sum(v > 0 for v in monthly.values()),
        symbol_r=sym_r,
        symbol_pnl=pnl,
        concentration=max(positive) / sum(positive) if positive else None,
        cash_days=len(daily) - len({t.day for t in trades}),
        costs=costs,
        cost_rejections=rejected,
        cost_rejections_by_symbol={
            sym: sum(rejected.get(sym, {}).values()) for sym in sorted(CALENDAR)
        },
        cost_rejections_total=sum(tiers.values()),
        cost_rejections_by_tier=dict(tiers),
        contention_by_symbol={sym: result.contention.get(sym, 0) for sym in sorted(CALENDAR)},
    )
