"""Aggregate D103 summary.json into the report tables (rules fixed in D103-structure.md).

Read-only: reads only summary.json (and the preserved metadata for symbol lists). Rules:
equal weight per symbol, nearest-rank quantiles, per-metric valid/excluded counts,
ratios and multiples computed per symbol first, then their median. Missing values are
never zero-filled.
"""

import argparse
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from richletsgo.research.d103.stats import quantile

D = Decimal
HALF, P90, P95 = D("0.5"), D("0.9"), D("0.95")
SESSIONS = ("regular", "pre", "post", "overnight", "break", "closed", "unknown")
TIERS = (">=200M", "50M-200M", "20M-50M", "5M-20M", "<5M", "uncomputable")


def num(value: Any) -> Decimal | None:
    return None if value is None else D(str(value))


def med(values: list[Decimal]) -> Decimal | None:
    return quantile(values, HALF)


def pct(value: Decimal | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def group_of(row: dict[str, Any]) -> str:
    kind = row["type"]
    return f"{kind}:{row['region']}" if kind in ("stock", "ETF") and row.get("region") else kind


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = json.loads(args.summary.read_text(encoding="utf-8"))
    symbols: dict[str, dict[str, Any]] = document["summary"]["symbols"]
    out: dict[str, Any] = {
        "T0": document.get("T0"),
        "allowlist_sha256": document.get("allowlist_sha256"),
        "data_hash": document.get("data_hash"),
        "symbols": len(symbols),
    }

    # 1. Turnover tiers and composition.
    tiers: dict[str, list[str]] = defaultdict(list)
    for name, row in symbols.items():
        tiers[row["tier"]].append(name)
    out["tiers"] = {t: sorted(tiers.get(t, [])) for t in sorted(tiers)}
    zero = [num(r["zero_volume_ratio"]) for r in symbols.values()]
    zero_valid = [z for z in zero if z is not None]
    out["zero_volume_ratio"] = {
        "median": med(zero_valid),
        "valid": len(zero_valid),
        "excluded": len(zero) - len(zero_valid),
    }
    out["tier_by_group"] = defaultdict(lambda: defaultdict(int))
    for row in symbols.values():
        out["tier_by_group"][group_of(row)][row["tier"]] += 1

    # 2. Spread by session.
    spread: dict[str, Any] = {}
    for session in SESSIONS:
        medians, p90s, empty_ratios = [], [], []
        samples = 0
        for row in symbols.values():
            cell = row["spread"].get(session)
            empty = int(row["spread_empty"].get(session, 0))
            count = int(cell["count"]) if cell else 0
            samples += count
            if cell and cell["median"] is not None:
                medians.append(num(cell["median"]))
                p90s.append(num(cell["p90"]))
            if count + empty:
                empty_ratios.append(D(empty) / (count + empty))
        spread[session] = {
            "valid": len(medians),
            "samples": samples,
            "median_of_medians": med(medians),
            "median_of_p90": med(p90s),
            "empty_ratio_median": med(empty_ratios),
        }
    out["spread"] = spread
    out["stale_top_total"] = sum(int(r["spread_empty"].get("stale", 0)) for r in symbols.values())

    # Depth cost by session/notional/side.
    depth: dict[str, Any] = defaultdict(dict)
    sessions_seen = sorted({s for r in symbols.values() for s in r["depth_cost"]})
    for session in sessions_seen:
        for notional in (1000, 5000, 20000):
            for side in ("buy", "sell"):
                key = f"{side}:{notional}"
                values = [
                    num(r["depth_cost"][session][key])
                    for r in symbols.values()
                    if session in r["depth_cost"] and r["depth_cost"][session].get(key) is not None
                ]
                unfilled = sum(
                    int(r["depth_unfilled"].get(f"{session}:{key}", 0)) for r in symbols.values()
                )
                depth[session][key] = {
                    "valid": len(values),
                    "median_of_medians": med(values),
                    "unfilled": unfilled,
                }
    out["depth"] = depth
    out["stale_depth_total"] = sum(
        int(r["depth_unfilled"].get("stale", 0)) for r in symbols.values()
    )
    out["empty_depth_by_session"] = {
        s: sum(int(r["depth_unfilled"].get(s, 0)) for r in symbols.values()) for s in SESSIONS
    }

    # Multiples vs regular, per symbol then median.
    multiples: dict[str, Any] = {}
    for session in SESSIONS:
        if session == "regular":
            continue
        ratios = []
        excluded = 0
        for row in symbols.values():
            base, other = row["spread"].get("regular"), row["spread"].get(session)
            if other is None:
                continue
            if (
                base
                and base["median"] is not None
                and num(base["median"]) > 0
                and other["median"] is not None
            ):
                ratios.append(num(other["median"]) / num(base["median"]))
            else:
                excluded += 1
        entry: dict[str, Any] = {
            "spread": {"median": med(ratios), "valid": len(ratios), "excluded": excluded}
        }
        for notional in (1000, 5000, 20000):
            for side in ("buy", "sell"):
                key = f"{side}:{notional}"
                ratios, excluded = [], 0
                for row in symbols.values():
                    base = row["depth_cost"].get("regular", {}).get(key)
                    other = row["depth_cost"].get(session, {}).get(key)
                    if other is None:
                        continue
                    if base is not None and num(base) > 0:
                        ratios.append(num(other) / num(base))
                    else:
                        excluded += 1
                entry[key] = {"median": med(ratios), "valid": len(ratios), "excluded": excluded}
        multiples[session] = entry
    out["multiples_vs_regular"] = multiples

    # 3. Reopen changes by boundary kind and group.
    reopen: dict[str, Any] = defaultdict(dict)
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in symbols.values():
        for kind, cell in row["reopen"].items():
            by[(kind, group_of(row))].append(cell)
            by[(kind, "all")].append(cell)
    for (kind, group), cells in sorted(by.items()):
        reopen[kind][group] = {
            "valid": len(cells),
            "count": sum(int(c["count"]) for c in cells),
            "median_of_medians": med([num(c["median"]) for c in cells]),
            "median_of_p95": med([num(c["p95"]) for c in cells]),
            "max_of_max": max(num(c["max"]) for c in cells),
        }
    out["reopen"] = reopen
    out["reopen_missing_total"] = sum(int(r["reopen_missing"]) for r in symbols.values())
    out["reopen_halted_total"] = sum(int(r["reopen_halted"]) for r in symbols.values())

    # 4. Flat index/mark runs during closed hours.
    flat: dict[str, Any] = defaultdict(dict)
    for kind in ("index", "mark"):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in symbols.values():
            cell = row["flat"][kind]
            groups[group_of(row)].append(cell)
            groups["all"].append(cell)
        for group, cells in sorted(groups.items()):
            valid = [c for c in cells if c["runs"]]
            flat[kind][group] = {
                "runs": sum(int(c["runs"]) for c in cells),
                "valid": len(valid),
                "median_of_medians": med([num(c["median_hours"]) for c in valid]),
                "median_of_p95": med(
                    [num(c["p95_hours"]) for c in valid if c.get("p95_hours") is not None]
                ),
                "max_of_max": max((int(c["max_hours"]) for c in cells), default=0),
            }
    out["flat"] = flat

    # 5. Funding by symbolType.
    funding: dict[str, Any] = {}
    groups2: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in symbols.values():
        groups2[row["type"]].append(row["funding"])
    for group, cells in sorted(groups2.items()):
        valid = [c for c in cells if c["count"]]
        funding[group] = {
            "valid": len(valid),
            "excluded": len(cells) - len(valid),
            "abs_median": med([num(c["abs_median"]) for c in valid]),
            "abs_p95": med([num(c["abs_p95"]) for c in valid if c.get("abs_p95") is not None]),
            "cap_ratio": med(
                [
                    num(c["at_current_cap_ratio"])
                    for c in valid
                    if c.get("at_current_cap_ratio") is not None
                ]
            ),
        }
    out["funding"] = funding

    args.output.write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
