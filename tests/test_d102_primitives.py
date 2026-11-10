"""Synthetic D102 funding and basket contract boundaries; no market DB."""

import pytest

from richletsgo.research.d98.boundaries import DAY, HOUR
from richletsgo.research.d98.models import D, FundingPoint
from richletsgo.research.d102.basket import BasketState, Candidate, targets
from richletsgo.research.d102.funding import FundingSeries


@pytest.mark.parametrize("hours", [1, 2, 4, 8])
def test_score_normalizes_each_settlement_interval_and_excludes_left_boundary(hours):
    points = [FundingPoint(t, D(hours) / 8 * D(".001")) for t in range(0, DAY + 1, hours * HOUR)]
    points[0] = FundingPoint(0, D("999"))
    assert FundingSeries(points).score(DAY, 1) == (D(".001"), None)


def test_score_uses_each_historical_interval_and_not_current_metadata():
    points = [FundingPoint(t, D(".001")) for t in range(0, 17 * HOUR, 8 * HOUR)]
    points.extend(FundingPoint(t, D(".000125")) for t in range(17 * HOUR, DAY + 1, HOUR))
    assert FundingSeries(points).score(DAY, 1) == (D(".001"), None)


@pytest.mark.parametrize(
    "times,at", [([HOUR, DAY], DAY), ([0, 3 * HOUR], DAY), ([0, 8 * HOUR], DAY)]
)
def test_score_rejects_missing_predecessor_invalid_interval_and_staleness(times, at):
    series = FundingSeries([FundingPoint(t, D(".001")) for t in times])
    assert series.has_input(at)
    assert series.score(at, 1) == (None, "funding_coverage")


def test_period_requires_both_endpoints_and_excludes_entry_settlement():
    series = FundingSeries([FundingPoint(t, D(".001")) for t in (0, 8 * HOUR, 16 * HOUR)])
    assert [p.time for p in series.period_points(0, 16 * HOUR)] == [8 * HOUR, 16 * HOUR]
    assert series.period_points(0, DAY) is not None
    assert series.period_points(0, DAY + 1) is None
    assert series.period_points(-1, HOUR) is None
    assert FundingSeries([]).has_input(DAY) is False


def test_all_ties_form_disjoint_legs_and_string_prefix_order():
    symbols = ["AB", "ABC", *[f"X{i:02}" for i in range(23)]]
    rows = [Candidate(s, D(0), D(".001")) for s in symbols]
    assert targets(rows[:24]) == {}
    result = targets(rows)
    assert len(result) == 10
    assert [s for s, w in result.items() if w < 0] == sorted(symbols)[:5]
    assert [s for s, w in result.items() if w > 0] == sorted(symbols)[-5:]
    assert sum(result.values(), D(0)) == 0
    assert sum(map(abs, result.values()), D(0)) == 1


def test_turnover_reversal_cash_and_tier_fallback():
    state = BasketState()
    assert state.rebalance({"A": D(".1")}, {"A": D(".001")}) == (D(".000155"), D(".1"))
    assert state.rebalance({"A": D(".1")}, {}) == (D(0), D(0))
    assert state.rebalance({"A": D("-.1")}, {}) == (D(".000310"), D(".2"))
    assert state.rebalance({}, {}) == (D(".000155"), D(".1"))
    assert state.holdings == {}


def test_final_liquidation_excludes_delisted_and_keeps_invalid_survivor_cost():
    state = BasketState()
    state.rebalance(
        {"DELIST": D(".1"), "INVALID": D("-.1")}, {"DELIST": D(".0015"), "INVALID": D(".0015")}
    )
    assert state.delist("DELIST") == D(".000055")
    assert state.delist("DELIST") == 0
    # INVALID contributes no PnL, but the state retains its original target.
    # No endpoint liquidity input is accepted: the retained t_last tier applies.
    assert state.liquidate() == D(".000205")
    assert state.liquidate() == 0
