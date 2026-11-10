"""D102 period and contribution concentration gates, with fixed denominators."""

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from richletsgo.research.d98.models import D, calculated


@calculated
def summarize(
    periods: list[dict[str, Any]], contributions: list[tuple[D, int, str]]
) -> dict[str, Any]:
    valid = [p for p in periods if p["kind"] != "invalid"]
    values = [D(p["net_return"]) for p in valid]
    n = len(values)
    total = sum(values, D(0))
    mean = total / n if n else None
    deviation = (
        (sum(((x - mean) ** 2 for x in values), D(0)) / (n - 1)).sqrt()
        if n > 1 and mean is not None
        else None
    )
    statistic = mean / (deviation / D(n).sqrt()) if deviation and mean is not None else None
    trim_count = (n + 99) // 100
    top_periods = sorted(valid, key=lambda p: (-D(p["net_return"]), p["t"]))[:trim_count]
    trimmed = (
        (total - sum((D(p["net_return"]) for p in top_periods), D(0))) / (n - trim_count)
        if n > trim_count
        else None
    )
    years: dict[str, list[D]] = defaultdict(list)
    months: dict[str, list[D]] = defaultdict(list)
    for p in valid:
        dt = datetime.fromtimestamp(p["t"] / 1000, UTC)
        years[str(dt.year)].append(D(p["net_return"]))
        months[dt.strftime("%Y-%m")].append(D(p["net_return"]))
    qualifying = [v for v in years.values() if len(v) >= 20]
    equity = peak = D(1)
    drawdown = D(0)
    bankrupt_at = None
    for p in valid:
        ret = D(p["net_return"])
        if bankrupt_at is None:
            if ret <= -1:
                bankrupt_at = p["t"]
                equity = D(0)
            else:
                equity *= 1 + ret
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    count = len(contributions)
    contribution_trim_count = (count + 99) // 100
    removed = sorted(contributions, key=lambda x: (-x[0], x[1], x[2]))[:contribution_trim_count]
    concentration_mean = (total - sum((x[0] for x in removed), D(0))) / n if n else None
    position_count = sum(p["position_count"] for p in periods)
    invalid_positions = sum(p["invalid_count"] for p in periods)
    cash_count = sum(p["kind"] == "cash" for p in periods)
    invalid_count = sum(p["kind"] == "invalid" for p in periods)
    undecidable = invalid_positions * 20 > position_count or (
        cash_count + invalid_count
    ) * 20 > len(periods)
    criteria = [
        n >= 100
        and mean is not None
        and mean > 0
        and (deviation == 0 or statistic is not None and statistic >= 2),
        trimmed is not None and trimmed > 0,
        len(qualifying) >= 3 and sum(sum(v, D(0)) > 0 for v in qualifying) * 2 > len(qualifying),
        drawdown < D(".25"),
        concentration_mean is not None and concentration_mean > 0,
    ]

    def bins(data: dict[str, list[D]]) -> dict[str, Any]:
        return {
            key: {"count": len(v), "sum": sum(v, D(0)), "mean": sum(v, D(0)) / len(v)}
            for key, v in sorted(data.items())
        }

    paired = [
        (D(p["btc_return"]), D(p["net_return"])) for p in valid if p["btc_return"] is not None
    ]
    correlation = beta = None
    if len(paired) > 1:
        mx = sum((x for x, y in paired), D(0)) / len(paired)
        my = sum((y for x, y in paired), D(0)) / len(paired)
        xx = sum(((x - mx) ** 2 for x, y in paired), D(0))
        yy = sum(((y - my) ** 2 for x, y in paired), D(0))
        xy = sum(((x - mx) * (y - my) for x, y in paired), D(0))
        beta = xy / xx if xx else None
        correlation = xy / (xx * yy).sqrt() if xx and yy else None
    return {
        "periods": len(periods),
        "valid": n,
        "cash": cash_count,
        "invalid": invalid_count,
        "positions": position_count,
        "invalid_positions": invalid_positions,
        "undecidable": undecidable,
        "mean": mean,
        "sample_sd": deviation,
        "t_statistic": statistic,
        "trim_count": trim_count,
        "trimmed_mean": trimmed,
        "trimmed_periods": [p["t"] for p in top_periods],
        "contribution_count": count,
        "contribution_trim_count": contribution_trim_count,
        "contribution_trimmed_mean": concentration_mean,
        "removed_contributions": [{"value": x, "t": t, "symbol": s} for x, t, s in removed],
        "years": bins(years),
        "months": bins(months),
        "max_drawdown": drawdown,
        "final_equity": equity,
        "bankrupt_at": bankrupt_at,
        "criteria_1_5": criteria,
        "candidate": not undecidable and all(criteria),
        "mean_positive": not undecidable and mean is not None and mean > 0,
        "technical": {
            "btc_pairs": len(paired),
            "btc_correlation": correlation,
            "btc_beta": beta,
            "worst_period": min(valid, key=lambda p: (D(p["net_return"]), p["t"]))
            if valid
            else None,
            **{
                key: sum((D(p[key]) for p in valid), D(0))
                for key in (
                    "price",
                    "funding",
                    "cost",
                    "turnover",
                    "long_contribution",
                    "short_contribution",
                )
            },
        },
    }


def verdict(combinations: dict[str, dict[str, Any]], eligible: int, insufficient: int) -> str:
    if len(combinations) != 4:
        raise ValueError("four combinations required")
    if insufficient * 20 > eligible:
        return "STUDY_UNDECIDABLE"
    if (
        any(row["candidate"] for row in combinations.values())
        and sum(row["mean_positive"] for row in combinations.values()) >= 2
    ):
        return "GO"
    return "NO_GO"
