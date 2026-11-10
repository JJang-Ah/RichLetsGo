"""Synthetic fixed-notional returns and chronological liquidation accounting."""

import pytest

from richletsgo.research.d98.boundaries import DAY, HOUR
from richletsgo.research.d98.models import D, FundingPoint, RawBar
from richletsgo.research.d102.basket import BasketState
from richletsgo.research.d102.eligibility import Series
from richletsgo.research.d102.period import contribution, outcome
from richletsgo.research.d102.portfolio import account_period


def bar(at, price=None, turnover=None):
    price = D(100) if price is None else price
    turnover = D(1_000_000) if turnover is None else turnover
    return RawBar(at, price, price, price, price, D(1), turnover)


def test_eligibility_priority_complete_history_and_both_windows():
    at = 100 * DAY
    bars = [bar(t) for t in range(at - 31 * DAY, at, HOUR)]
    funding = [FundingPoint(t, D(".001")) for t in range(at - 7 * DAY, at + 1, 8 * HOUR)]
    assert Series(bars, funding).classify(at, 0)[0] == "eligible"
    assert Series(bars[:-1], funding).classify(at, 0)[0] == "input_insufficient"
    assert Series(bars, []).classify(at, 0)[0] == "input_insufficient"
    assert Series(bars, funding).classify(at, at - 59 * DAY)[0] == "metadata_ineligible"
    zero = [bar(b.start, turnover=D(0)) for b in bars]
    assert Series(zero, funding[-1:]).classify(at, 0)[0] == "zero_mean"
    low = [bar(b.start, turnover=D(1)) for b in bars]
    assert Series(low, funding[-1:]).classify(at, 0)[0] == "liquidity"
    # One-day score passes but the seven-day predecessor is absent.
    assert Series(bars, funding[1:]).classify(at, 0)[0] == "funding_coverage"
    assert Series(bars, funding).turnover_mean(at) == 24_000_000
    # Most recent day is not part of D1..D30.
    inflated = [bar(b.start, turnover=D("999999999")) if b.start >= at - DAY else b for b in bars]
    assert Series(inflated, funding).turnover_mean(at) == 24_000_000


def test_liquidity_twenty_million_is_inclusive():
    at = 100 * DAY
    # An exact daily sum avoids recurring 20M/24 Decimal rounding in the fixture.
    bars = [
        bar(t, turnover=D(20_000_000) if t % DAY == 0 else D(0))
        for t in range(at - 31 * DAY, at, HOUR)
    ]
    funding = [FundingPoint(t, D(".001")) for t in range(at - 7 * DAY, at + 1, 8 * HOUR)]
    stage, features = Series(bars, funding).classify(at, 0)
    assert stage == "eligible" and features["slippage"] == D(".0015")
    bars[0] = bar(bars[0].start, turnover=D(19_999_999))
    assert Series(bars, funding).classify(at, 0)[0] == "liquidity"


def test_required_prices_only_and_funding_price_ratio():
    bars = [bar(0), bar(8 * HOUR, D(110)), bar(16 * HOUR, D(120))]
    points = [FundingPoint(t, D(".001")) for t in (0, 8 * HOUR, 16 * HOUR)]
    coverage = {"tail": "none", "data_end": 30 * HOUR}
    raw = outcome(Series(bars, points), coverage, 0, 16 * HOUR)
    assert contribution(raw, 1) == (D(".2"), D("-.0023"))
    assert contribution(raw, -1) == (D("-.2"), D(".0023"))
    missing = outcome(Series([bars[0], bars[2]], points), coverage, 0, 16 * HOUR)
    assert missing["invalid"] == "funding_price_missing"
    assert contribution(missing, 1) == (0, 0)


@pytest.mark.parametrize("end", [16 * HOUR, 24 * HOUR])
def test_delisting_endpoint_funding_close_price_and_no_later_funding(end):
    bars = [bar(0), bar(8 * HOUR, D(110)), bar(15 * HOUR, D(120))]
    points = [FundingPoint(t, D(".001")) for t in (0, 8 * HOUR, 16 * HOUR, 24 * HOUR)]
    coverage = {"tail": "allowed", "data_end": 16 * HOUR, "L_a": 15 * HOUR}
    raw = outcome(Series(bars, points), coverage, 0, end)
    assert raw["delisted"] and raw["invalid"] is None
    assert contribution(raw, 1) == (D(".176"), D("-.0023"))
    assert contribution(raw, -1) == (D("-.224"), D(".0023"))


def candidates():
    return {
        f"S{i:02}": {
            "score1": str(i),
            "score7": str(i),
            "slippage": ".001",
            "outcomes": {
                "3": {
                    "ratio": "1.1",
                    "funding": str(D(i) / 10000),
                    "delisted": False,
                    "invalid": None,
                    "exit_time": 3 * DAY,
                }
            },
        }
        for i in range(25)
    }


def run_period(state, rows, at=0, final=False):
    return account_period(
        state, rows, lookback=1, holding=3, at=at, end=at + 3 * DAY, final=final, btc_return=D(".1")
    )


def test_market_neutral_same_price_movement_and_hand_calculated_funding():
    row, positions = run_period(BasketState(), candidates(), final=True)
    assert row["price"] == 0
    assert row["funding"] == D(".001")  # .1 * ((20+21+22+23+24)-(0+1+2+3+4))/10000
    assert row["cost"] == D(".00310")
    assert row["kind"] == "normal"
    assert len(positions) == 10


def test_invalid_symbol_no_redistribution_and_whole_leg_invalid_retains_targets():
    state = BasketState()
    rows = candidates()
    rows["S00"]["outcomes"]["3"]["invalid"] = "funding_gap"
    row, positions = run_period(state, rows)
    assert row["kind"] == "normal" and row["invalid_count"] == 1
    assert all(p["weight"] == D(".1") for p in positions)
    assert row["cost"] == D(".00155")
    for i in range(5):
        rows[f"S{i:02}"]["outcomes"]["3"]["invalid"] = "price_missing"
    row, _ = run_period(state, rows, at=3 * DAY)
    assert row["kind"] == "invalid" and row["net_return"] is None
    assert row["cost"] == 0 and len(state.holdings) == 10
    cash, _ = run_period(state, {}, at=6 * DAY)
    assert cash["kind"] == "cash" and cash["net_return"] == D("-.00155")


def test_final_delist_cost_once_and_invalid_survivor_final_cost_preserved():
    rows = candidates()
    rows["S00"]["outcomes"]["3"]["delisted"] = True
    rows["S01"]["outcomes"]["3"]["invalid"] = "price_missing"
    state = BasketState()
    row, _ = run_period(state, rows, final=True)
    assert row["cost"] == D(".00155") + D(".1") * D(".00055") + D(".9") * D(".00155")
    assert state.holdings == {}
