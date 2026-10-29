"""Decimal-only selection and additive out-of-sample statistics."""

from collections import Counter
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .codec import digest, primitive
from .wf_config import utc

if TYPE_CHECKING:
    from .results import BacktestResult

ZERO = Decimal(0)


def drawdown(values: list[Decimal]) -> tuple[Decimal, Decimal]:
    if not values:
        return ZERO, ZERO
    peak, amount, ratio = values[0], ZERO, ZERO
    for value in values:
        peak = max(peak, value)
        amount = max(amount, peak - value)
        if peak > 0:
            ratio = max(ratio, (peak - value) / peak)
    return amount, ratio


def pause_counts(rows: tuple[dict[str, Any], ...]) -> dict[str, int]:
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    for row in rows:
        for pause in row["active"]:
            key = digest(pause)
            if key not in seen:
                counts[pause["kind"]] += 1
                seen.add(key)
    return dict(sorted(counts.items()))


def summarize(result: "BacktestResult", input_key: str) -> dict[str, Any]:
    initial = Decimal(result.metadata["config"]["profile"]["start_equity"])
    curve = [Decimal(r["equity"]) for r in result.equity]
    dd, ratio = drawdown([initial] + curve)
    trades = [t for t in result.trades if t["closed_at"] is not None]
    return dict(
        primitive(
            {
                "status": result.status,
                "issues": result.issues,
                "remaining_positions": result.remaining_positions,
                "input_key": input_key,
                "net_pnl": curve[-1] - initial if curve else ZERO,
                "max_drawdown": dd,
                "max_drawdown_ratio": ratio,
                "final_equity": curve[-1] if curve else initial,
                "trade_counts": {s: sum(t["strategy"] == s for t in trades) for s in ("S1", "S2")},
                "pause_counts": pause_counts(result.pauses),
                "forced_closes": sum("end_of_range" in t["reasons"] for t in trades),
                "trades_hash": digest(result.trades),
                "result_hash": result.result_hash,
            }
        )
    )


def choose(
    rows: list[tuple[Any, dict[str, Any]]], floor: Decimal, minimum: dict[str, int]
) -> tuple[Any, dict[str, Any]] | None:
    eligible = [
        (param, r)
        for param, r in rows
        if r["status"] == "COMPLETED"
        and not r["remaining_positions"]
        and all(r["trade_counts"][s] >= n for s, n in minimum.items())
    ]
    if not eligible:
        return None
    # param is the numeric S1 tuple followed by the numeric S2 tuple.
    return min(
        eligible,
        key=lambda item: (
            -Decimal(item[1]["net_pnl"]) / max(Decimal(item[1]["max_drawdown"]), floor),
            -sum(item[1]["trade_counts"].values()),
            item[0],
        ),
    )


def trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    profits = [Decimal(t["net_pnl"]) for t in trades]
    wins, losses = [x for x in profits if x > 0], [x for x in profits if x < 0]
    streak = maximum = 0
    for x in profits:
        streak = streak + 1 if x < 0 else 0
        maximum = max(maximum, streak)
    ordered = sorted(profits)
    return dict(
        primitive(
            {
                "trades": len(trades),
                "net_pnl": sum(profits, ZERO),
                "win_rate": Decimal(len(wins)) / len(trades) if trades else None,
                "average_win_loss_ratio": (sum(wins, ZERO) / len(wins))
                / (-sum(losses, ZERO) / len(losses))
                if wins and losses
                else None,
                "average_r": sum((Decimal(t["r_multiple"]) for t in trades), ZERO) / len(trades)
                if trades
                else None,
                "max_consecutive_losses": maximum,
                "pnl_distribution": {
                    str(q): ordered[(len(ordered) - 1) * q // 100] if ordered else None
                    for q in (0, 25, 50, 75, 100)
                },
            }
        )
    )


def aggregate(
    windows: list[dict[str, Any]],
    initial: Decimal,
    invalid_ratio: Decimal,
    criteria: dict[str, Decimal] | None,
    *,
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
    strategies: tuple[str, ...] = ("S1", "S2"),
    strategy_minimum: dict[str, Decimal] | None = None,
) -> dict[str, Any]:
    value = initial
    curve: list[dict[str, str]] = []
    monthly: dict[str, Decimal] = {}
    trades: list[dict[str, Any]] = []
    invalid = 0
    for window in windows:
        if window["status"] != "COMPLETED":
            invalid += 1
            # Flat throughout an invalid/no-candidate test interval; no interpolation.
            at, end = utc(window["start"]), utc(window["end"])
            while at <= end:
                curve.append({"at": at.isoformat(), "equity": str(value)})
                if at > utc(window["start"]):
                    monthly.setdefault((at - timedelta(microseconds=1)).strftime("%Y-%m"), ZERO)
                at += timedelta(hours=1)
            continue
        previous = initial
        for row in window["equity"]:
            current = Decimal(row["equity"])
            change = current - previous
            value += change
            at = utc(row["at"])
            month = (at - timedelta(microseconds=1)).strftime("%Y-%m")
            monthly[month] = monthly.get(month, ZERO) + change
            curve.append({"at": row["at"], "equity": str(value)})
            previous = current
        trades.extend(window["trades"])
    trades.sort(key=lambda t: (t["closed_at"], t["symbol"], t["strategy"], t["episode_id"]))
    dd, ratio = drawdown([initial] + [Decimal(r["equity"]) for r in curve])
    status = (
        "INDETERMINATE"
        if Decimal(invalid) / len(windows) > invalid_ratio
        else "UNSET"
        if criteria is None
        else (
            "PASS"
            if value - initial > criteria["min_net_pnl_usdt"]
            and ratio < criteria["max_drawdown_ratio"]
            else "FAIL"
        )
    )
    if status == "PASS" and strategy_minimum is not None:
        if any(
            sum((Decimal(t["net_pnl"]) for t in trades if t["strategy"] == name), ZERO) <= threshold
            for name, threshold in strategy_minimum.items()
        ):
            status = "FAIL"
    result = trade_stats(trades)
    result.update(
        primitive(
            {
                "net_pnl": value - initial,
                "max_drawdown": dd,
                "max_drawdown_ratio": ratio,
                "invalid_windows": invalid,
                "verdict": status,
                "monthly_pnl": monthly,
                "equity": curve,
                "trade_records": trades,
                "by_strategy": {
                    s: trade_stats([t for t in trades if t["strategy"] == s])
                    if s in strategies
                    else {}
                    for s in ("S1", "S2")
                },
                "by_symbol": {
                    s: trade_stats([t for t in trades if t["symbol"] == s]) for s in symbols
                },
            }
        )
    )
    return result
