"""Market-structure statistics only: no directional, holding or signal returns (M2-10 5)."""

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from .allowlist import Allowlist
from .models import AFTER, BEFORE, DAY, HOUR, NOTIONALS, STALE_AFTER
from .sessions import Boundary, Sessions, calendar_for, load, reopen_points
from .store import Store

D = Decimal
TIERS = (
    (D(200_000_000), "200M+"),
    (D(50_000_000), "50M-200M"),
    (D(20_000_000), "20M-50M"),
    (D(5_000_000), "5M-20M"),
)


def quantile(values: Sequence[Decimal], q: Decimal) -> Decimal | None:
    """Nearest-rank quantile; deterministic and exact on Decimal inputs."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int((q * len(ordered)).to_integral_value(rounding="ROUND_CEILING")))
    return ordered[rank - 1]


def tier(mean: Decimal | None) -> str:
    if mean is None:
        return "uncomputable"
    for threshold, name in TIERS:
        if mean >= threshold:
            return name
    return "<5M"


def daily_turnover(hourly: dict[int, Decimal]) -> dict[int, Decimal]:
    """Only days with all 24 hourly bars present; missing bars are never zero-filled."""
    days: dict[int, list[Decimal]] = defaultdict(list)
    for start, value in hourly.items():
        days[start // DAY * DAY].append(value)
    return {day: sum(v, D(0)) for day, v in days.items() if len(v) == 24}


def rolling_means(daily: dict[int, Decimal]) -> dict[int, Decimal]:
    """Mean of 30 consecutive complete days ending the day before each key (D_1..D_30)."""
    out = {}
    for day in sorted(daily):
        at = day + DAY
        window = [at - j * DAY for j in range(1, 31)]
        if all(d in daily for d in window):
            out[at] = sum((daily[d] for d in window), D(0)) / 30
    return out


def spread(bid: str | None, ask: str | None) -> Decimal | None:
    if bid is None or ask is None:
        return None
    b, a = D(bid), D(ask)
    mid = (a + b) / 2
    return (a - b) / mid if mid > 0 else None


def depth_cost(
    levels: Sequence[Sequence[str]], mid: Decimal, notional: Decimal, side: str
) -> Decimal | None:
    """Non-negative instant cost vs same-snapshot mid; None when depth is insufficient."""
    if mid <= 0 or side not in ("buy", "sell"):
        raise ValueError("positive mid and buy/sell side required")
    remaining, quantity = notional, D(0)
    for price_text, qty_text in levels:
        price, qty = D(price_text), D(qty_text)
        take = min(qty, remaining / price)
        quantity += take
        remaining -= take * price
        if remaining <= 0:
            vwap = notional / quantity
            return (vwap - mid) / mid if side == "buy" else (mid - vwap) / mid
    return None


def reopen_changes(
    closes: dict[int, Decimal],
    boundaries: Iterable[Boundary],
    halts: Sequence[Sequence[int]] = (),
) -> tuple[list[tuple[str, str, Decimal]], int, int]:
    """abs(P_after / P_before - 1) per R; (values, missing, halted).

    A missing bar skips and counts as missing. A window [R - 5m, R + 15m) overlapping an
    announced halt [start, end) is excluded and counted apart, even with both bars present.
    """
    out, missing, halted = [], 0, 0
    for b in boundaries:
        low, high = b.at + BEFORE[0], b.at + AFTER[1]
        if any(start < high and low < end for start, end in halts):
            halted += 1
            continue
        before = closes.get(b.at + BEFORE[0])
        after = closes.get(b.at + AFTER[1] - 5 * 60_000)
        if before is None or after is None or before <= 0:
            missing += 1
            continue
        out.append((b.label, b.previous, abs(after / before - 1)))
    return out, missing, halted


def load_halts(document: dict[str, Any]) -> dict[str, list[tuple[int, int]]]:
    """Announced trading halts per symbol; every interval needs a recorded source."""
    if not document.get("checked_at") or not isinstance(document.get("halts"), dict):
        raise ValueError("halts file needs checked_at and a halts mapping")
    out: dict[str, list[tuple[int, int]]] = {}
    for symbol, rows in document["halts"].items():
        for row in rows:
            start, end, source = int(row["start"]), int(row["end"]), str(row.get("source") or "")
            if not start < end or not source:
                raise ValueError(f"invalid halt for {symbol}")
            out.setdefault(symbol, []).append((start, end))
    return out


def book_label(sessions: Sessions, requested: int, ts: int) -> str:
    """Session of the book's own time; a ts far from the request start is stale."""
    if abs(ts - requested) > STALE_AFTER:
        return "stale"
    return sessions.label(ts)


def flat_runs(bars: dict[int, tuple[Decimal, ...]], sessions: Sessions) -> list[int]:
    """Lengths (hours) of consecutive closed-session hourly bars with O=H=L=C.

    A missing bar, a non-flat bar or a non-closed hour ends the run.
    """
    runs: list[int] = []
    current, previous = 0, None
    for start in sorted(bars):
        o, h, low, c = bars[start]
        flat = o == h == low == c and sessions.label(start) == "closed"
        if flat and current and previous is not None and start == previous + HOUR:
            current += 1
        else:
            if current:
                runs.append(current)
            current = 1 if flat else 0
        previous = start
    if current:
        runs.append(current)
    return runs


def flat_summary(runs: Sequence[int]) -> dict[str, Any]:
    """Run count and nearest-rank median/p95/max of flat-run lengths in hours."""
    lengths = [D(r) for r in runs]
    return {
        "runs": len(runs),
        "max_hours": max(runs, default=0),
        "median_hours": quantile(lengths, D("0.5")),
        "p95_hours": quantile(lengths, D("0.95")),
    }


def funding_summary(rates: Sequence[Decimal], upper: str) -> dict[str, Any]:
    absolute = [abs(r) for r in rates]
    cap = D(upper) if upper else None
    return {
        "count": len(rates),
        "abs_median": quantile(absolute, D("0.5")),
        "abs_p95": quantile(absolute, D("0.95")),
        # Past caps are unknown; this is relative to the current metadata cap only.
        "at_current_cap_ratio": (
            D(sum(a >= cap for a in absolute)) / len(absolute) if absolute and cap else None
        ),
    }


def summarize(
    store: Store,
    allowlist: Allowlist,
    table: dict[str, Any],
    halts: dict[str, list[tuple[int, int]]],
) -> dict[str, Any]:
    symbols: dict[str, Any] = {}
    sessions_cache: dict[str, Sessions] = {}
    for symbol in sorted(allowlist.symbols):
        entry = allowlist.entries[symbol]
        name = calendar_for(entry)
        sessions = sessions_cache.setdefault(name, load(table, name))
        hourly = {
            int(s): D(t)
            for s, t in store.rows(
                "SELECT start, turnover FROM bars WHERE kind='trade' AND interval='60' "
                "AND symbol=?",
                (symbol,),
            )
        }
        volumes = [
            D(v)
            for (v,) in store.rows(
                "SELECT volume FROM bars WHERE kind='trade' AND interval='60' AND symbol=?",
                (symbol,),
            )
        ]
        means = rolling_means(daily_turnover(hourly))
        latest = means[max(means)] if means else None
        closes = {
            int(s): D(c)
            for s, c in store.rows(
                "SELECT start, close FROM bars WHERE kind='trade' AND interval='5' AND symbol=?",
                (symbol,),
            )
        }
        changes, missing, halted = reopen_changes(
            closes, reopen_points(sessions, entry.launch), halts.get(symbol, [])
        )
        flat = {}
        for kind in ("index", "mark"):
            bars = {
                int(r[0]): tuple(D(x) for x in r[1:])
                for r in store.rows(
                    "SELECT start, open, high, low, close FROM bars WHERE kind=? "
                    "AND interval='60' AND symbol=?",
                    (kind, symbol),
                )
            }
            flat[kind] = flat_summary(flat_runs(bars, sessions))
        spreads: dict[str, list[Decimal]] = defaultdict(list)
        empty: dict[str, int] = defaultdict(int)
        for requested, ts, bid, ask in store.rows(
            "SELECT requested, ts, bid, ask FROM book_top WHERE symbol=?", (symbol,)
        ):
            label = book_label(sessions, int(requested), int(ts))
            if label == "stale":
                empty[label] += 1
                continue
            value = spread(bid, ask)
            if value is None:
                empty[label] += 1
            else:
                spreads[label].append(value)
        costs: dict[str, dict[str, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
        unfilled: dict[str, int] = defaultdict(int)
        for requested, ts, bids_text, asks_text in store.rows(
            "SELECT requested, ts, bids, asks FROM book_depth WHERE symbol=?", (symbol,)
        ):
            label = book_label(sessions, int(requested), int(ts))
            if label == "stale":
                unfilled[label] += 1
                continue
            bids, asks = json.loads(bids_text), json.loads(asks_text)
            if not bids or not asks:
                unfilled[label] += 1
                continue
            mid = (D(bids[0][0]) + D(asks[0][0])) / 2
            for notional in NOTIONALS:
                for side, levels in (("buy", asks), ("sell", bids)):
                    cost = depth_cost(levels, mid, D(notional), side)
                    if cost is None:
                        unfilled[f"{label}:{side}:{notional}"] += 1
                    else:
                        costs[label][f"{side}:{notional}"].append(cost)
        rates = [D(r) for (r,) in store.rows("SELECT rate FROM funding WHERE symbol=?", (symbol,))]
        by_boundary: dict[str, list[Decimal]] = defaultdict(list)
        for label, previous, value in changes:
            by_boundary[f"{previous}->{label}"].append(value)
        symbols[symbol] = {
            "type": entry.symbol_type,
            "region": entry.region,
            "calendar": name,
            "hourly_bars": len(hourly),
            "zero_volume_ratio": (
                D(sum(v == 0 for v in volumes)) / len(volumes) if volumes else None
            ),
            "turnover_30d_latest": latest,
            "turnover_30d_median": quantile(list(means.values()), D("0.5")),
            "tier": tier(latest),
            "reopen": {
                k: {
                    "count": len(v),
                    "median": quantile(v, D("0.5")),
                    "p95": quantile(v, D("0.95")),
                    "max": max(v),
                }
                for k, v in sorted(by_boundary.items())
            },
            "reopen_missing": missing,
            "reopen_halted": halted,
            "flat": flat,
            "spread": {
                k: {"count": len(v), "median": quantile(v, D("0.5")), "p90": quantile(v, D("0.9"))}
                for k, v in sorted(spreads.items())
            },
            "spread_empty": dict(sorted(empty.items())),
            "depth_cost": {
                label: {k: quantile(v, D("0.5")) for k, v in sorted(group.items())}
                for label, group in sorted(costs.items())
            },
            "depth_unfilled": dict(sorted(unfilled.items())),
            "funding": funding_summary(rates, entry.upper_funding),
        }
    tiers: dict[str, int] = defaultdict(int)
    for row in symbols.values():
        tiers[row["tier"]] += 1
    return {"symbols": symbols, "tiers": dict(sorted(tiers.items()))}
