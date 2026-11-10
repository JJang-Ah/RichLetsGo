"""Fixed-notional symbol-period outcomes; costs are charged by the basket owner."""

from typing import Any

from richletsgo.research.d98.models import D, calculated

from .eligibility import Series


@calculated
def outcome(series: Series, coverage: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    delisted = coverage["tail"] == "allowed" and int(coverage["data_end"]) <= end
    exit_at = int(coverage["data_end"]) if delisted else end
    result: dict[str, Any] = {
        "delisted": delisted,
        "exit_time": exit_at,
        "invalid": None,
        "ratio": None,
        "funding": None,
    }
    entry = series.bars.get(start)
    exit_bar = series.bars.get(coverage["L_a"] if delisted else end)
    if exit_at <= start or entry is None or exit_bar is None:
        return {**result, "invalid": "price_missing"}
    points = series.funding.period_points(start, exit_at)
    if points is None:
        return {**result, "invalid": "funding_gap"}
    paid = D(0)
    for point in points:
        bar = series.bars.get(point.time)
        if delisted and point.time == exit_at:
            price = exit_bar.close
        elif bar is None:
            return {**result, "invalid": "funding_price_missing"}
        else:
            price = bar.open
        paid += point.rate * price / entry.open
    reference = exit_bar.close if delisted else exit_bar.open
    return {**result, "ratio": reference / entry.open, "funding": paid}


@calculated
def contribution(raw: dict[str, Any], direction: int) -> tuple[D, D]:
    if direction not in (-1, 1):
        raise ValueError("direction must be +/-1")
    if raw["invalid"]:
        return D(0), D(0)
    ratio = D(raw["ratio"])
    if raw["delisted"]:
        ratio *= 1 - direction * D(".02")
    return direction * (ratio - 1), -direction * D(raw["funding"])
