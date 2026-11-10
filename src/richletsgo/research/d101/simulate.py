"""One-unit D101 trade path, isolated from OMS and live order code."""

from bisect import bisect_right
from decimal import Decimal
from typing import Any

from richletsgo.research.d98.boundaries import DAY, HOUR
from richletsgo.research.d98.models import FEE, D, calculated

from .models import FUNDING_GAP, HOLD, Combination, adjust_stop, stop_price
from .signals import Series, reversal


def _invalid(reason: str, at: int) -> tuple[None, str, int]:
    return None, reason, at


@calculated
def simulate(
    series: Series,
    coverage: dict[str, Any],
    symbol: str,
    at: int,
    direction: int,
    combo: Combination,
    features: dict[str, Any],
    btc_state: str,
) -> tuple[dict[str, Any] | None, str | None, int]:
    """Return trade, invalid reason and release time (after the bar for intrabar stops)."""
    if direction not in (-1, 1):
        raise ValueError("direction must be +/-1")
    signal_day = series.daily.get(at - DAY)
    entry_bar = series.bars.get(at)
    if signal_day is None:
        return _invalid("signal_daily", at)
    if entry_bar is None:
        return _invalid("future_bars", at)
    stop = stop_price(series.daily, at, combo.m, direction)
    risk = direction * (signal_day.close - stop)
    if risk <= 0:
        return _invalid("nonpositive_risk", at)

    slip = features["slippage"]
    opening = entry_bar.open
    entry = opening * (1 + direction * slip)
    funding = D(0)
    funding_by_time = {point.time: point.rate for point in series.funding}
    funding_index = bisect_right(series.funding_times, at) - 1
    if funding_index < 0:
        return _invalid("future_funding", at)
    last_funding = series.funding_times[funding_index]
    if at - last_funding > FUNDING_GAP:
        return _invalid("future_funding", at)

    # A gap through the initial stop is a same-open entry/exit and remains a valid trade.
    if direction * (opening - stop) <= 0:
        gap_price = opening * (1 - direction * slip)
        return (
            _trade(
                symbol,
                at,
                direction,
                entry,
                gap_price,
                opening,
                opening,
                at,
                "gap",
                risk,
                funding,
                slip,
                combo,
                btc_state,
                str(features["liquidity_bucket"]),
            ),
            None,
            at,
        )

    end = int(coverage["data_end"])
    tail_allowed = coverage["tail"] == "allowed"
    last_bar_start = coverage.get("L_a")
    last_funding_index = funding_index
    exit_time: int | None = None
    exit_reason = ""
    intrabar_exit = False
    price: Decimal | None = None
    reference: Decimal | None = None

    for u in range(at, min(at + HOLD, end) + HOUR, HOUR):
        if u >= end:
            if not tail_allowed or last_bar_start is None or last_bar_start not in series.bars:
                return _invalid("future_bars", u)
            last_bar = series.bars[last_bar_start]
            # Settlement at E_a is applied at close(L_a); later settlements are excluded.
            rate = funding_by_time.get(end)
            if rate is not None:
                if end - last_funding > FUNDING_GAP:
                    return _invalid("future_funding", end)
                funding += direction * rate * last_bar.close
                last_funding = end
            if end - last_funding > FUNDING_GAP:
                return _invalid("future_funding", end)
            reference = last_bar.close
            price = reference * (1 - direction * D("0.02"))
            exit_time, exit_reason = end, "delist"
            break

        bar = series.bars.get(u)
        if bar is None:
            return _invalid("future_bars", u)

        # Funding comes before midnight events and the stop check. Entry t is excluded.
        for point_index in range(last_funding_index + 1, bisect_right(series.funding_times, u)):
            tau = series.funding_times[point_index]
            if tau <= at:
                last_funding_index = point_index
                continue
            if tau != u or tau not in series.bars:
                return _invalid("funding_price_missing", tau)
            if tau - last_funding > FUNDING_GAP:
                return _invalid("future_funding", tau)
            funding += direction * funding_by_time[tau] * bar.open
            last_funding = tau
            last_funding_index = point_index
        if u - last_funding > FUNDING_GAP:
            return _invalid("future_funding", u)

        event: str | None = None
        if u > at and u % DAY == 0:
            if reversal(series, u, direction, combo.n):
                event = "reverse"
            elif u - at >= HOLD:
                event = "H"
            else:
                try:
                    candidate = stop_price(series.daily, u, combo.m, direction)
                except ValueError:
                    return _invalid("future_daily", u)
                stop = adjust_stop(stop, candidate, direction)

        if event is not None:
            if direction * (bar.open - stop) <= 0:
                reference = bar.open
                price = reference * (1 - direction * slip)
                exit_reason = "stop"
            else:
                reference = bar.open
                price = reference * (1 - direction * slip)
                exit_reason = event
            exit_time = u
            break
        if direction * (bar.open - stop) <= 0:
            reference = bar.open
            price = reference * (1 - direction * slip)
            exit_time, exit_reason = u, "gap"
            break
        if bar.low <= stop if direction == 1 else bar.high >= stop:
            reference = stop
            price = reference * (1 - direction * slip)
            exit_time, exit_reason = u, "stop"
            intrabar_exit = True
            break

    if exit_time is None or price is None or reference is None:
        return _invalid("future_bars", min(at + HOLD, end))
    trade = _trade(
        symbol,
        at,
        direction,
        entry,
        price,
        opening,
        reference,
        exit_time,
        exit_reason,
        risk,
        funding,
        slip,
        combo,
        btc_state,
        str(features["liquidity_bucket"]),
    )
    # exit_time labels the bar, not the actual intrabar fill time. Its opening
    # cannot be reused for entry when the position closes later in that bar.
    return trade, None, exit_time + HOUR if intrabar_exit else exit_time


@calculated
def _trade(
    symbol: str,
    at: int,
    direction: int,
    entry: Decimal,
    price: Decimal,
    entry_reference: Decimal,
    reference: Decimal,
    exit_time: int,
    exit_reason: str,
    risk: Decimal,
    funding: Decimal,
    slip: Decimal,
    combo: Combination,
    btc_state: str,
    bucket: str,
) -> dict[str, Any]:
    fees = FEE * (entry + price)
    pnl = direction * (price - entry) - fees - funding
    slippage_cost = direction * (entry - entry_reference) + direction * (reference - price)
    return {
        "symbol": symbol,
        "combination": combo.key,
        "d": direction,
        "t": at,
        "P_e": entry,
        "P_x": price,
        "exit_time": exit_time,
        "exit_reason": exit_reason,
        "R": pnl / risk,
        "R0": risk,
        "funding": funding,
        "fees": fees,
        "slippage": slip,
        "slippage_cost": slippage_cost,
        "holding_hours": (exit_time - at) // HOUR,
        "btc_state": btc_state,
        "liquidity_bucket": bucket,
        "pnl": pnl,
    }
