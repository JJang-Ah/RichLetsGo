"""Independent one-unit D98 trades; no OMS, account or live order path."""

from bisect import bisect_right
from typing import Any, Protocol

from .boundaries import DAY, HOUR
from .models import FEE, HOLD, Combination, D, calculated
from .signals import Series


class Trend(Protocol):
    def at(self, t: int) -> int: ...


def future_invalid(series: Series, t: int, data_end: int) -> str | None:
    end = min(t + HOLD + HOUR, data_end)
    if not series.complete(t, end):
        return "future_bars"
    settle_end = min(t + HOLD, data_end)
    begin = bisect_right(series.funding_times, t) - 1
    if begin < 0:
        return "future_funding"
    last = bisect_right(series.funding_times, settle_end)
    times = series.funding_times[begin:last]
    if (
        not times
        or settle_end - times[-1] > 8 * HOUR
        or any(b - a > 8 * HOUR for a, b in zip(times, times[1:], strict=False))
    ):
        return "future_funding"
    # Funding requires a bar opening at tau, except the explicit delist close.
    if any(u > t and u != data_end and u not in series.bars for u in times):
        return "funding_price_missing"
    return None


@calculated
def simulate(
    series: Series,
    trend: Trend,
    coverage: dict[str, Any],
    symbol: str,
    t: int,
    d: int,
    combo: Combination,
    features: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    end = coverage["data_end"]
    reason = future_invalid(series, t, end)
    if reason:
        return None, reason
    if d not in (-1, 1):
        raise ValueError("direction must be +/-1")
    slip = features["slippage"]
    risk = combo.k * features["atr"]
    if risk <= 0:
        raise ValueError("positive fixed risk required")
    opening = series.bars[t].open
    entry = opening * (1 + d * slip)
    stop = series.four[t].close - d * risk
    funding = D(0)
    extreme = None
    price = None
    exit_at = t
    exit_reason = ""
    rates = {
        p.time: p.rate
        for p in series.funding[
            bisect_right(series.funding_times, t) : bisect_right(
                series.funding_times, min(t + HOLD, end)
            )
        ]
    }
    for u in range(t, t + HOLD + 1, HOUR):
        if u >= end and coverage["tail"] == "allowed":
            last = series.bars[coverage["L_a"]]
            funding += d * rates.get(end, D(0)) * last.close
            price = last.close * (1 - d * D("0.02"))
            exit_at = end
            exit_reason = "delist"
            break
        if u not in series.bars:
            return None, "future_bars"
        bar = series.bars[u]
        funding += d * rates.get(u, D(0)) * bar.open
        time_exit = None
        if u > t and u % (4 * HOUR) == 0:
            if trend.at(u) != d:
                time_exit = "btc_transition"
            elif u - t >= HOLD:
                time_exit = "H"
            if time_exit is not None:
                gap = d * (bar.open - stop) <= 0
                price = bar.open * (1 - d * slip)
                exit_reason = "stop" if gap else time_exit
                exit_at = u
                break
            closed = series.four[u].close
            extreme = (
                closed
                if extreme is None
                else max(extreme, closed)
                if d == 1
                else min(extreme, closed)
            )
            candidate = extreme - d * combo.k * series.atr_at(u)
            stop = max(stop, candidate) if d == 1 else min(stop, candidate)
        if d * (bar.open - stop) <= 0:
            price = bar.open * (1 - d * slip)
            exit_at = u
            exit_reason = "gap"
            break
        if bar.low <= stop if d == 1 else bar.high >= stop:
            price = stop * (1 - d * slip)
            exit_at = u
            exit_reason = "stop"
            break
    if price is None:
        return None, "unclosed"
    pnl = d * (price - entry) - FEE * (entry + price) - funding
    observations = {}
    for days in (1, 3, 7):
        u = t + days * DAY
        observations[str(days)] = (
            d * (series.bars[u].open - opening) / opening if u < end and u in series.bars else None
        )
    return {
        "combination": combo.key,
        "symbol": symbol,
        "t": t,
        "d": d,
        "P_e": entry,
        "P_x": price,
        "exit_time": exit_at,
        "exit_reason": exit_reason,
        "funding": funding,
        "risk": risk,
        "pnl": pnl,
        "R": pnl / risk,
        "slippage": slip,
        "forward_returns": observations,
    }, None
