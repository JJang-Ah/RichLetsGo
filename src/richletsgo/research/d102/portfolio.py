"""Chronological basket accounting, independent of storage and worker scheduling."""

from typing import Any

from richletsgo.research.d98.models import D, calculated

from .basket import BasketState, Candidate, targets
from .period import contribution


@calculated
def account_period(
    state: BasketState,
    candidates: dict[str, dict[str, Any]],
    *,
    lookback: int,
    holding: int,
    at: int,
    end: int,
    final: bool,
    btc_return: D | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    target = targets(
        [
            Candidate(symbol, D(row[f"score{lookback}"]), D(row["slippage"]))
            for symbol, row in sorted(candidates.items())
        ]
    )
    cost, turnover = state.rebalance(
        target, {s: D(row["slippage"]) for s, row in candidates.items()}
    )
    positions: list[dict[str, Any]] = []
    for symbol, signed in sorted(target.items()):
        raw = candidates[symbol]["outcomes"][str(holding)]
        direction = 1 if signed > 0 else -1
        weight = abs(signed)
        g, f = contribution(raw, direction)
        if raw["delisted"]:
            cost += state.delist(symbol)
            turnover += weight
        positions.append(
            {
                "t": at,
                "symbol": symbol,
                "weight": weight,
                "direction": direction,
                "g": g,
                "f": f,
                "contribution": weight * (g + f),
                "invalid": raw["invalid"],
                "delisted": raw["delisted"],
                "exit_time": raw["exit_time"],
                "score": D(candidates[symbol][f"score{lookback}"]),
                "slippage": D(candidates[symbol]["slippage"]),
            }
        )
    if final:
        turnover += sum((abs(value) for value in state.holdings.values()), D(0))
        cost += state.liquidate()
    good_legs = {p["direction"] for p in positions if p["invalid"] is None}
    kind = "cash" if not target else "normal" if good_legs == {-1, 1} else "invalid"
    price = sum((p["weight"] * p["g"] for p in positions), D(0))
    funding = sum((p["weight"] * p["f"] for p in positions), D(0))
    row = {
        "t": at,
        "end": end,
        "kind": kind,
        "candidates": len(candidates),
        "k": len(target) // 2,
        "position_count": len(positions),
        "invalid_count": sum(p["invalid"] is not None for p in positions),
        "invalid_symbols": [p["symbol"] for p in positions if p["invalid"] is not None],
        "price": price,
        "funding": funding,
        "cost": cost,
        "turnover": turnover,
        "net_return": None if kind == "invalid" else price + funding - cost,
        "btc_return": btc_return,
    }
    for direction, label in ((1, "long"), (-1, "short")):
        leg = [p for p in positions if p["direction"] == direction]
        row[label] = [p["symbol"] for p in leg]
        row[label + "_contribution"] = sum((p["contribution"] for p in leg), D(0))
        row[label + "_score_mean"] = (
            sum((p["score"] for p in leg), D(0)) / len(leg) if leg else None
        )
    return row, positions
