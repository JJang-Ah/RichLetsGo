"""07c formulas, timing and full synthetic pipeline regressions."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import localcontext

import pytest
from d98_fixtures import T, bar, collected_dataset, daily_bars, dataset

from richletsgo.domain import Symbol
from richletsgo.marketdata.models import Bar, Interval
from richletsgo.research.d98.audit import audit
from richletsgo.research.d98.boundaries import DAY, HOUR
from richletsgo.research.d98.btc_trend import SEED, BTCTrend
from richletsgo.research.d98.models import Combination, D, FundingPoint
from richletsgo.research.d98.signals import (
    Series,
    atr,
    ema,
    four_hour_bars,
    funding_state,
    slippage,
)
from richletsgo.research.d98.simulate import future_invalid, simulate
from richletsgo.research.d98.store import Store
from richletsgo.research.d98.study import study
from richletsgo.strategy.history import ClosedBars
from richletsgo.strategy.indicators import atr as existing_atr


def series(end=T + 170 * HOUR):
    bars = [bar(t) for t in range(T - 800 * HOUR, end, HOUR)]
    funding = [FundingPoint(t, D(0)) for t in range(T - 808 * HOUR, end + 1, 4 * HOUR)]
    return Series(bars, funding)


class Trend:
    def __init__(self, change=None):
        self.change = change

    def at(self, t):
        return -1 if self.change is not None and t >= self.change else 1


def cov(end=T + 170 * HOUR, allowed=False):
    return {"data_end": end, "tail": "allowed" if allowed else "not_applicable", "L_a": end - HOUR}


def features(slip="0"):
    return {"slippage": D(slip), "atr": D("10")}


def test_aggregation_ema_and_atr_cross_check():
    hours = [bar(t) for t in range(0, 8 * HOUR, HOUR)]
    assert len(four_hour_bars(hours)) == 2
    assert len(four_hour_bars(hours[:2] + hours[3:])) == 1
    assert ema([bar(i * 4 * HOUR, "100") for i in range(200)]) == D(100)
    seq = [bar(i * 4 * HOUR, str(100 + i), high=str(110 + i), low=str(90 + i)) for i in range(200)]
    with localcontext() as ctx:
        ctx.prec = 34
        expected = D("109.5")
        alpha = D(2) / 21
        for b in seq[20:]:
            expected = alpha * b.close + (1 - alpha) * expected
    assert ema(seq) == expected
    converted = tuple(
        Bar(
            Symbol.BTCUSDT,
            Interval.FOUR_HOUR,
            datetime(2022, 1, 1, tzinfo=UTC) + timedelta(hours=i * 4),
            b.open,
            b.high,
            b.low,
            b.close,
            b.volume,
            b.turnover,
        )
        for i, b in enumerate(seq[-140:])
    )
    history = ClosedBars(Symbol.BTCUSDT, Interval.FOUR_HOUR, converted, converted[-1].end)
    assert atr(seq[-140:]) == existing_atr(history, 14)


@pytest.mark.parametrize("hours", [1, 2, 4, 8])
def test_funding_interval_and_freshness(hours):
    step = hours * HOUR
    points = [
        FundingPoint(T - 2 * step, D("0.0001")),
        FundingPoint(T - step, D("0.0001")),
        FundingPoint(T, D("0.0001")),
    ]
    assert funding_state(points, T + step) == (D("0.0001") * 8 / hours, None)
    assert funding_state(points, T + step + 1)[1] == "funding_stale"
    assert funding_state(points, T)[0] == D("0.0001") * 8 / hours
    points[0] = FundingPoint(T - 3 * step, D(0))
    assert funding_state(points, T)[1] == "funding_interval"


def test_historical_interval_transition_ignores_current_metadata():
    points = [FundingPoint(T + i * 4 * HOUR, D("0.0002")) for i in range(-3, 1)]
    points += [FundingPoint(T + i * HOUR, D("0.0002")) for i in range(1, 4)]
    s = Series([], points)
    assert funding_state(s.past_funding(T), T)[0] == D("0.0004")
    assert funding_state(s.past_funding(T + HOUR), T + HOUR)[1] == "funding_interval"
    assert funding_state(s.past_funding(T + 2 * HOUR), T + 2 * HOUR)[0] == D("0.0016")


def test_volume_window_liquidity_and_strict_signal():
    s = series()
    for t in range(T - DAY, T, HOUR):
        s.bars[t] = replace(s.bars[t], turnover=D("6000000"))
    s = Series(list(s.bars.values()), s.funding)
    f, reason = s.features(T)
    assert reason is None and f["V"] == 6 and f["mean_turnover"] == 24_000_000
    assert f["slippage"] == D("0.0015")
    assert not s.signal(T, 1, 2, 42, f)
    s.four[T] = replace(s.four[T], close=D(105), high=D(999))
    assert not s.signal(T, 1, 2, 42, f)  # equality with prior highs is not breakout
    s.four[T] = replace(s.four[T], close=D(106))
    assert s.signal(T, 1, 2, 42, f)  # current high 999 is excluded
    f["funding_8h"] = D("0.0005")
    assert not s.signal(T, 1, 2, 42, f)
    empty = Series([replace(b, turnover=D(0)) for b in s.bars.values()], s.funding)
    assert empty.features(T)[1] == "zero_median"
    assert slippage(D("50000000")) == D("0.0010")
    assert slippage(D("4999999")) is None


def test_btc_seed_warmup_and_midnight():
    trend = BTCTrend(daily_bars())
    assert trend.at(SEED) == 0
    assert trend.at(SEED + DAY - 1) == 0
    assert trend.at(SEED + DAY) == 1
    assert trend.at(T) == 1
    with pytest.raises(ValueError, match="BTC_INCOMPLETE"):
        BTCTrend(daily_bars()[1:])


def test_h_exact_and_funding_endpoints():
    s = series()
    s.funding = [FundingPoint(T, D("0.5")), FundingPoint(T + 168 * HOUR, D("0.001"))] + [
        p for p in s.funding if p.time not in (T, T + 168 * HOUR)
    ]
    s = Series(list(s.bars.values()), s.funding)
    trade, reason = simulate(s, Trend(), cov(), "SYN", T, 1, Combination(2, 42, 2), features())
    assert reason is None and trade["exit_time"] == T + 168 * HOUR and trade["exit_reason"] == "H"
    assert trade["funding"] == D("0.1")  # entry settlement excluded, exit settlement included
    assert trade["risk"] == 20
    assert trade["R"] == (-D("0.11") - D("0.1")) / 20
    s.bars.pop(T + 168 * HOUR)
    s = Series(list(s.bars.values()), s.funding)
    assert future_invalid(s, T, cov()["data_end"]) == "future_bars"


@pytest.mark.parametrize("d", [-1, 1])
def test_gap_and_intrabar_stop(d):
    s = series()
    feature = {"slippage": D("0.001"), "atr": D(1)}
    s.bars[T] = bar(T, opening="97" if d == 1 else "103")
    trade, _ = simulate(s, Trend(), cov(), "SYN", T, d, Combination(2, 42, 2), feature)
    assert trade["exit_reason"] == "gap" and trade["exit_time"] == T
    s.bars[T] = bar(T)
    trade, _ = simulate(s, Trend(), cov(), "SYN", T, d, Combination(2, 42, 2), feature)
    assert trade["exit_reason"] == "stop" and trade["P_x"] == (D(100) - d * 2) * (
        1 - d * D("0.001")
    )


def test_btc_time_exit_gap_priority_and_nonretroactive_trail():
    s = series()
    s.bars[T + 4 * HOUR] = bar(T + 4 * HOUR, opening="79", low="75")
    trade, _ = simulate(
        s, Trend(T + 4 * HOUR), cov(), "SYN", T, 1, Combination(2, 42, 2), features()
    )
    assert trade["exit_reason"] == "stop" and trade["exit_time"] == T + 4 * HOUR
    s = series()
    for u in range(T, T + 4 * HOUR, HOUR):
        s.bars[u] = bar(u, "120", opening="100", high="125", low="90")
    s = Series(list(s.bars.values()), s.funding)
    s.atr_cache[T + 4 * HOUR] = D(2)
    trade, _ = simulate(s, Trend(), cov(), "SYN", T, 1, Combination(2, 42, 2), features())
    assert (
        trade["exit_time"] == T + 4 * HOUR and trade["exit_reason"] == "gap"
    )  # no retrospective stop at 116
    s = series()
    trade, _ = simulate(
        s, Trend(T + 4 * HOUR), cov(), "SYN", T, 1, Combination(2, 42, 2), features()
    )
    assert trade["exit_reason"] == "btc_transition"


def test_delist_tail_funding_and_missing_horizon():
    end = T + 8 * HOUR
    s = series(end)
    s.funding = [replace(p, rate=D("0.001")) if p.time == end else p for p in s.funding]
    s = Series(list(s.bars.values()), s.funding)
    trade, reason = simulate(
        s, Trend(), cov(end, True), "SYN", T, 1, Combination(2, 42, 2), features("0.0025")
    )
    assert reason is None and trade["P_x"] == 98 and trade["exit_reason"] == "delist"
    assert trade["funding"] == D("0.1")
    assert trade["forward_returns"] == {"1": None, "3": None, "7": None}
    assert (
        simulate(s, Trend(), cov(end + 30 * HOUR), "SYN", T, 1, Combination(2, 42, 2), features())[
            1
        ]
        == "future_bars"
    )


def test_full_pipeline_delisting_rate_hash_r_and_workers(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    p, a = collected_dataset(path)
    result = study(p, a, path, tmp_path / "one", workers=1, registration_digest="synthetic")
    parallel = study(p, a, path, tmp_path / "two", workers=2, registration_digest="synthetic")
    assert result == parallel
    assert (tmp_path / "one/D98-study-trades.jsonl").read_bytes() == (
        tmp_path / "two/D98-study-trades.jsonl"
    ).read_bytes()
    assert result["body"]["trades"]["count"] > 0
    import json

    before = [
        json.loads(s) for s in (tmp_path / "one/D98-study-trades.jsonl").read_text().splitlines()
    ]
    st = Store(path)
    with st.db:
        st.db.execute("UPDATE funding SET rate='0.001' WHERE time=?", (T + 8 * HOUR,))
    st.close()
    changed = audit(p, path)
    assert changed["body"]["data_hash"] != a["body"]["data_hash"]
    study(p, changed, path, tmp_path / "changed", workers=1, registration_digest="synthetic")
    after = [
        json.loads(s)
        for s in (tmp_path / "changed/D98-study-trades.jsonl").read_text().splitlines()
    ]
    pairs = [(x, y) for x, y in zip(before, after, strict=True) if x["exit_reason"] == "delist"]
    assert pairs
    for x, y in pairs:
        assert D(y["funding"]) - D(x["funding"]) == D("0.12")
        with localcontext() as ctx:
            ctx.prec = 34
            assert D(y["R"]) == (D(y["pnl"]) / D(y["risk"]))
        assert D(y["R"]) < D(x["R"])
    assert result["body"]["stages"]["metadata_eligible"] == a["body"]["metadata_eligible"]
    assert result["body"]["stages"]["input_insufficient"] == a["body"]["input_insufficient"]


def test_overlapping_contracts_and_judgment_not_read(tmp_path):
    from d98_fixtures import contract, make_pool

    from richletsgo.backtest.codec import digest

    path = tmp_path / "synthetic.sqlite"
    p, _ = dataset(path)
    secondary = contract("SYN2USDT")
    secondary["priority"][0] = 1000
    judgment = contract("JUDGMENTUSDT", key="UNSEEN")
    judgment["pool"] = "JUDGMENT"
    p = make_pool([p["body"]["contracts"][0], secondary, judgment])
    st = Store(path)
    with st.db:
        st.db.execute("UPDATE identity SET value=? WHERE key='pool'", (p["sha256"],))
        st.put("bars", "SYN2USDT", list(st.rows("bars", "SYNUSDT")))
        st.put("funding", "SYN2USDT", list(st.rows("funding", "SYNUSDT")))
        # Poison rows: any accidental JUDGMENT read fails RawBar validation.
        st.db.execute("INSERT INTO bars_1h VALUES ('JUDGMENTUSDT',0,'poison','x','x','x','x','x')")
    st.close()
    a = audit(p, path)
    secondary_audit = next(c for c in a["body"]["contracts"] if c["symbol"] == "SYN2USDT")
    assert secondary_audit["opportunities"] == 0
    result = study(p, a, path, tmp_path / "overlap", workers=1, registration_digest="synthetic")
    assert (
        result["body"]["stages"]["opportunities"]
        == next(c for c in a["body"]["contracts"] if c["symbol"] == "SYNUSDT")["opportunities"]
    )
    assert "SYN2USDT" not in (tmp_path / "overlap/D98-study-trades.jsonl").read_text()
    assert "JUDGMENTUSDT" not in (tmp_path / "overlap/D98-study-trades.jsonl").read_text()
    assert digest(p["body"]) == p["sha256"]


def test_future_funding_coverage_eight_hour_boundaries():
    s = series()
    s.funding = [p for p in s.funding if p.time <= T + 160 * HOUR]
    s = Series(list(s.bars.values()), s.funding)
    assert future_invalid(s, T, cov()["data_end"]) is None  # exactly 8h tail
    s = Series(list(s.bars.values()), s.funding[:-1])
    assert future_invalid(s, T, cov()["data_end"]) == "future_funding"
    s = series()
    s = Series(
        list(s.bars.values()), [p for p in s.funding if not T + 4 * HOUR <= p.time <= T + 8 * HOUR]
    )
    assert future_invalid(s, T, cov()["data_end"]) == "future_funding"


def test_short_signal_and_trail_never_widens():
    s = series()
    f, _ = s.features(T)
    f["V"] = D(3)
    s.four[T] = replace(s.four[T], close=D(94), low=D(1))
    assert s.signal(T, -1, 3, 84, f)
    f["funding_8h"] = D("-0.0005")
    assert not s.signal(T, -1, 3, 84, f)
    # Stop tightens to 96 at u=4h; later ATR expansion must not widen it.
    s = series()
    for u in range(T, T + 8 * HOUR, HOUR):
        s.bars[u] = bar(u, "90", opening="90", high="91", low="89")
    s.bars[T + 8 * HOUR] = bar(T + 8 * HOUR, "97", high="98", low="96")
    s = Series(list(s.bars.values()), s.funding)
    s.atr_cache[T + 4 * HOUR] = D(3)
    s.atr_cache[T + 8 * HOUR] = D(20)
    trade, reason = simulate(s, Trend(T), cov(), "SYN", T, -1, Combination(2, 42, 2), features())
    assert reason is None and trade["exit_time"] == T + 8 * HOUR
    assert trade["exit_reason"] == "gap" and trade["P_x"] == 97
