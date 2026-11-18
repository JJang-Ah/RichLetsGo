"""D103 report aggregation over a synthetic summary document (no real data)."""

from decimal import Decimal as D
from typing import Any

from richletsgo.research.d103.tables import build


def symbol(
    tier: str,
    spread: dict[str, str | None],
    depth: dict[str, dict[str, str]],
    unfilled: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "type": "stock",
        "region": "US",
        "tier": tier,
        "zero_volume_ratio": None,
        "spread": {s: {"count": 4, "median": m, "p90": m} for s, m in spread.items()},
        "spread_empty": {},
        "depth_cost": depth,
        "depth_unfilled": unfilled or {},
        "reopen": {},
        "reopen_missing": 0,
        "reopen_halted": 0,
        "flat": {
            "index": {"runs": 0, "max_hours": 0, "median_hours": None, "p95_hours": None},
            "mark": {"runs": 0, "max_hours": 0, "median_hours": None, "p95_hours": None},
        },
        "funding": {"count": 0},
    }


def document() -> dict[str, Any]:
    key = "buy:20000"
    return {
        "summary": {
            "symbols": {
                # both sessions valid: closed/regular = 2
                "XQAUSDT": symbol(
                    "50M-200M",
                    {"regular": "0.001", "closed": "0.002"},
                    {"regular": {key: "0.0003"}, "closed": {key: "0.0006"}},
                ),
                # regular base is zero: excluded, not dropped
                "XQBUSDT": symbol(
                    "50M-200M",
                    {"regular": "0", "closed": "0.004"},
                    {"regular": {key: "0"}, "closed": {key: "0.0009"}},
                    {f"closed:{key}": 2},
                ),
                # no closed session at all: not applicable
                "XQCUSDT": symbol(
                    "5M-20M",
                    {"regular": "0.003"},
                    {"regular": {key: "0.0012"}},
                    {f"regular:{key}": 1},
                ),
                # closed entry exists but has no valid median: excluded
                "XQDUSDT": symbol(
                    "<5M",
                    {"regular": "0.005", "closed": None},
                    {"regular": {key: "0.002"}},
                ),
            }
        }
    }


def test_multiples_account_for_every_symbol() -> None:
    closed = build(document())["multiples_vs_regular"]["closed"]
    assert closed["spread"] == {
        "median": D("2"),
        "population": 4,
        "not_applicable": 1,
        "excluded": 2,
        "valid": 1,
    }
    depth = closed["buy:20000"]
    assert depth["valid"] == 1 and depth["median"] == D("2")
    assert depth["excluded"] == 1  # XQBUSDT: zero base
    assert depth["not_applicable"] == 2  # XQCUSDT, XQDUSDT have no closed depth
    assert depth["population"] == 4


def test_cost_by_tier_keeps_valid_and_unfilled_counts() -> None:
    by_tier = build(document())["cost_by_tier"]
    assert set(by_tier) == {"50M-200M", "5M-20M"}  # <5M is not a cost tier
    top = by_tier["50M-200M"]
    assert top["symbols"] == 2
    closed = top["sessions"]["closed"]["buy:20000"]
    assert closed == {
        "population": 2,
        "valid": 2,
        "median_of_medians": D("0.0006"),
        "unfilled": 2,
    }
    assert top["sessions"]["regular"]["spread"]["valid"] == 2
    small = by_tier["5M-20M"]["sessions"]
    assert set(small) == {"regular"}
    assert small["regular"]["buy:20000"]["unfilled"] == 1
