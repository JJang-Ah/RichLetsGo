"""Aggregate a D103 summary document into report tables (rules fixed in D103-structure.md).

Pure function over the published ``summary.json`` document: equal weight per symbol,
nearest-rank quantiles, per-metric population/valid/excluded counts, ratios and multiples
computed per symbol first and then their median. Missing values are never zero-filled.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal
from functools import partial
from typing import Any

from richletsgo.research.d103.stats import quantile

D = Decimal
HALF = D("0.5")
SESSIONS = ("regular", "pre", "post", "overnight", "break", "closed", "unknown")
NOTIONALS = (1000, 5000, 20000)
SIDES = ("buy", "sell")
DEPTH_KEYS = tuple(f"{side}:{notional}" for notional in NOTIONALS for side in SIDES)
# Tiers whose depth cost is reported per session (D99 slippage is tier-specific).
COST_TIERS = (">=200M", "50M-200M", "20M-50M", "5M-20M")

Row = Mapping[str, Any]


def num(value: Any) -> Decimal | None:
    return None if value is None else D(str(value))


def need(value: Any) -> Decimal:
    result = num(value)
    if result is None:
        raise ValueError("expected a value")
    return result


def med(values: Iterable[Decimal]) -> Decimal | None:
    return quantile(list(values), HALF)


def group_of(row: Row) -> str:
    kind = str(row["type"])
    region = row.get("region")
    return f"{kind}:{region}" if kind in ("stock", "ETF") and region else kind


def spread_cell(rows: Iterable[Row], session: str) -> dict[str, Any]:
    medians: list[Decimal] = []
    p90s: list[Decimal] = []
    empty_ratios: list[Decimal] = []
    samples = 0
    population = 0
    for row in rows:
        population += 1
        cell = row["spread"].get(session)
        empty = int(row["spread_empty"].get(session, 0))
        count = int(cell["count"]) if cell else 0
        samples += count
        if cell and cell["median"] is not None:
            medians.append(need(cell["median"]))
            p90s.append(need(cell["p90"]))
        if count + empty:
            empty_ratios.append(D(empty) / (count + empty))
    return {
        "population": population,
        "valid": len(medians),
        "samples": samples,
        "median_of_medians": med(medians),
        "median_of_p90": med(p90s),
        "empty_ratio_median": med(empty_ratios),
    }


def depth_cell(rows: Iterable[Row], session: str, key: str) -> dict[str, Any]:
    values: list[Decimal] = []
    unfilled = 0
    population = 0
    for row in rows:
        population += 1
        value = row["depth_cost"].get(session, {}).get(key)
        if value is not None:
            values.append(need(value))
        unfilled += int(row["depth_unfilled"].get(f"{session}:{key}", 0))
    return {
        "population": population,
        "valid": len(values),
        "median_of_medians": med(values),
        "unfilled": unfilled,
    }


def multiple_cell(
    rows: Iterable[Row],
    present: Callable[[Row], bool],
    base_of: Callable[[Row], Decimal | None],
    other_of: Callable[[Row], Decimal | None],
) -> dict[str, Any]:
    """Per-symbol ``other / base`` then median, with every symbol accounted for.

    ``not_applicable``: the symbol has no entry for the session. ``excluded``: it has one,
    but its value is missing or the regular base is missing or zero.
    ``population = not_applicable + excluded + valid``.
    """
    ratios: list[Decimal] = []
    population = not_applicable = excluded = 0
    for row in rows:
        population += 1
        if not present(row):
            not_applicable += 1
            continue
        base, other = base_of(row), other_of(row)
        if base is not None and base > 0 and other is not None:
            ratios.append(other / base)
        else:
            excluded += 1
    return {
        "median": med(ratios),
        "population": population,
        "not_applicable": not_applicable,
        "excluded": excluded,
        "valid": len(ratios),
    }


def _spread_median(row: Row, session: str) -> Decimal | None:
    cell = row["spread"].get(session)
    return None if cell is None else num(cell["median"])


def _depth(row: Row, session: str, key: str) -> Decimal | None:
    return num(row["depth_cost"].get(session, {}).get(key))


def _has_spread(row: Row, session: str) -> bool:
    return row["spread"].get(session) is not None


def _has_depth(row: Row, session: str, key: str) -> bool:
    return _depth(row, session, key) is not None


def build(document: Mapping[str, Any]) -> dict[str, Any]:
    symbols: dict[str, Row] = document["summary"]["symbols"]
    rows = list(symbols.values())
    out: dict[str, Any] = {
        "T0": document.get("T0"),
        "allowlist_sha256": document.get("allowlist_sha256"),
        "data_hash": document.get("data_hash"),
        "symbols": len(symbols),
    }

    # 1. Turnover tiers and composition.
    tiers: dict[str, list[str]] = defaultdict(list)
    for name, row in symbols.items():
        tiers[str(row["tier"])].append(name)
    out["tiers"] = {t: sorted(tiers[t]) for t in sorted(tiers)}
    zero = [num(r["zero_volume_ratio"]) for r in rows]
    zero_valid = [z for z in zero if z is not None]
    out["zero_volume_ratio"] = {
        "median": med(zero_valid),
        "valid": len(zero_valid),
        "excluded": len(zero) - len(zero_valid),
    }
    by_group: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        by_group[group_of(row)][str(row["tier"])] += 1
    out["tier_by_group"] = by_group

    # 2. Spread and depth cost by session (all symbols).
    out["spread"] = {s: spread_cell(rows, s) for s in SESSIONS}
    out["stale_top_total"] = sum(int(r["spread_empty"].get("stale", 0)) for r in rows)
    sessions_seen = sorted({s for r in rows for s in r["depth_cost"]})
    out["depth"] = {s: {k: depth_cell(rows, s, k) for k in DEPTH_KEYS} for s in sessions_seen}
    out["stale_depth_total"] = sum(int(r["depth_unfilled"].get("stale", 0)) for r in rows)
    out["empty_depth_by_session"] = {
        s: sum(int(r["depth_unfilled"].get(s, 0)) for r in rows) for s in SESSIONS
    }

    # Multiples vs regular, per symbol then median.
    multiples: dict[str, Any] = {}
    for session in SESSIONS:
        if session == "regular":
            continue
        entry: dict[str, Any] = {
            "spread": multiple_cell(
                rows,
                partial(_has_spread, session=session),
                partial(_spread_median, session="regular"),
                partial(_spread_median, session=session),
            )
        }
        for key in DEPTH_KEYS:
            entry[key] = multiple_cell(
                rows,
                partial(_has_depth, session=session, key=key),
                partial(_depth, session="regular", key=key),
                partial(_depth, session=session, key=key),
            )
        multiples[session] = entry
    out["multiples_vs_regular"] = multiples

    # Spread and depth cost by turnover tier and session (D99 slippage is per tier).
    by_tier: dict[str, Any] = {}
    for name in COST_TIERS:
        members = [r for r in rows if r["tier"] == name]
        if not members:
            continue
        member_sessions = sorted(
            {s for r in members for s in r["depth_cost"]}
            | {s for r in members for s in r["spread"]}
        )
        by_tier[name] = {
            "symbols": len(members),
            "sessions": {
                s: {
                    "spread": spread_cell(members, s),
                    **{k: depth_cell(members, s, k) for k in DEPTH_KEYS},
                }
                for s in member_sessions
            },
        }
    out["cost_by_tier"] = by_tier

    # 3. Reopen changes by boundary kind and group.
    reopen: dict[str, Any] = defaultdict(dict)
    cells_by: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in rows:
        for kind, cell in row["reopen"].items():
            cells_by[(kind, group_of(row))].append(cell)
            cells_by[(kind, "all")].append(cell)
    for (kind, group), cells in sorted(cells_by.items()):
        reopen[kind][group] = {
            "valid": len(cells),
            "count": sum(int(c["count"]) for c in cells),
            "median_of_medians": med(need(c["median"]) for c in cells),
            "median_of_p95": med(need(c["p95"]) for c in cells),
            "max_of_max": max(need(c["max"]) for c in cells),
        }
    out["reopen"] = reopen
    out["reopen_missing_total"] = sum(int(r["reopen_missing"]) for r in rows)
    out["reopen_halted_total"] = sum(int(r["reopen_halted"]) for r in rows)

    # 4. Runs of consecutive closed-session hourly bars whose own OHLC are all equal.
    flat: dict[str, Any] = defaultdict(dict)
    for kind in ("index", "mark"):
        groups: dict[str, list[Row]] = defaultdict(list)
        for row in rows:
            cell = row["flat"][kind]
            groups[group_of(row)].append(cell)
            groups["all"].append(cell)
        for group, cells in sorted(groups.items()):
            valid = [c for c in cells if c["runs"]]
            flat[kind][group] = {
                "runs": sum(int(c["runs"]) for c in cells),
                "valid": len(valid),
                "median_of_medians": med(need(c["median_hours"]) for c in valid),
                "median_of_p95": med(
                    need(c["p95_hours"]) for c in valid if c.get("p95_hours") is not None
                ),
                "max_of_max": max((int(c["max_hours"]) for c in cells), default=0),
            }
    out["flat"] = flat

    # 5. Funding by symbolType.
    funding: dict[str, Any] = {}
    by_type: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_type[str(row["type"])].append(row["funding"])
    for group, cells in sorted(by_type.items()):
        valid = [c for c in cells if c["count"]]
        funding[group] = {
            "valid": len(valid),
            "excluded": len(cells) - len(valid),
            "abs_median": med(need(c["abs_median"]) for c in valid),
            "abs_p95": med(need(c["abs_p95"]) for c in valid if c.get("abs_p95") is not None),
            "cap_ratio": med(
                need(c["at_current_cap_ratio"])
                for c in valid
                if c.get("at_current_cap_ratio") is not None
            ),
        }
    out["funding"] = funding
    return out
