"""M2-02 hand-computed indicators and pure candidate contracts."""

import asyncio
import json
from dataclasses import MISSING, fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from decimal import localcontext

import pytest

from richletsgo.domain import Price, StrategyId, Symbol
from richletsgo.marketdata.models import Bar, Interval
from richletsgo.risk.records import Direction
from richletsgo.strategy import candidates as c
from richletsgo.strategy.history import ClosedBars
from richletsgo.strategy.indicators import Bands, atr, bollinger, channel, rsi, sma
from richletsgo.strategy.models import (
    S1AParams,
    S1BParams,
    S1Result,
    S1State,
    S2AParams,
    S2BParams,
    S2Episode,
    S2Result,
    Trend,
)

BTC = Symbol.BTCUSDT
START = datetime(2026, 1, 2, tzinfo=UTC)
A1 = S1AParams(1, 2, 1)
B1 = S1BParams(2, 1)
A2 = S2AParams(3, D(1), 2, D(1), 10)
B2 = S2BParams(2, D(30), D(70), D(50), 2, D(1), 10)


def history(closes, interval=Interval.HOUR):
    bars = tuple(
        Bar(
            BTC, interval, START + i * interval.duration, D(v), D(v) + 1, D(v) - 1, D(v), D(1), D(v)
        )
        for i, v in enumerate(closes)
    )
    return ClosedBars(BTC, interval, bars, bars[-1].end if bars else START)


def state(trend=Trend.FLAT, stop=None):
    return S1State(
        BTC,
        trend,
        Price(D(stop)) if stop is not None else None,
        START - timedelta(days=1) if trend is not Trend.FLAT else None,
    )


def test_hand_calculated_indicators():
    h = history([10, 12])
    assert sma(h, 2) == 11
    assert bollinger(h, 2, D(2)) == Bands(D(11), D(13), D(9))
    bounds = channel(history([10, 12, 20]), 2, include_current=False)
    assert (bounds.high, bounds.low) == (13, 9)
    assert channel(history([10, 12, 20]), 2, include_current=True).high == 21
    # Changes +2,-1,+2 => avg gain=4/3, avg loss=1/3 => RSI=80.
    assert abs(rsi(history([10, 12, 11, 13]), 3) - 80) < D("1e-28")
    # Next change -1: smoothed gain=8/9, loss=5/9 => RSI=800/13.
    assert abs(rsi(history([10, 12, 11, 13, 12]), 3) - D(800) / 13) < D("1e-24")
    h = history([10, 13, 14, 14])
    bars = list(h.bars)
    bars[2] = replace(bars[2], low=D(12))
    h = replace(h, bars=tuple(bars))
    # TRs=2,4,3,2. Seed=3; next=(3*2+2)/3=8/3.
    assert atr(h.through(h.bars[2].start), 3) == 3
    assert abs(atr(h, 3) - D(8) / 3) < D("1e-26")


@pytest.mark.parametrize(
    "closes,expected", [([10, 10, 10], 50), ([10, 11, 12], 100), ([12, 11, 10], 0)]
)
def test_rsi_degenerate_series(closes, expected):
    assert rsi(history(closes), 2) == expected


def test_warmup_and_fixed_decimal_context():
    h = history([10])
    assert sma(h, 2) is bollinger(h, 2, D(1)) is rsi(h, 1) is atr(h, 2) is None
    assert channel(h, 1, include_current=False) is None
    h = history([10, 12, 11, 13, 12])
    expected = (sma(h, 3), bollinger(h, 3, D(2)), rsi(h, 3), atr(h, 3))
    with localcontext() as ctx:
        ctx.prec = 5
        assert (sma(h, 3), bollinger(h, 3, D(2)), rsi(h, 3), atr(h, 3)) == expected


@pytest.mark.parametrize("n", [0, -1, True, 1.5])
def test_indicator_period_validation(n):
    for calc in (sma, rsi, atr):
        with pytest.raises(ValueError):
            calc(history([10]), n)


def test_history_rejects_unfinished_mixed_gap_and_unordered():
    h = history([100, 101])
    with pytest.raises(ValueError, match="unfinished"):
        replace(h, as_of=h.bars[-1].end - timedelta(microseconds=1))
    with pytest.raises(ValueError, match="mixed"):
        replace(h, bars=(replace(h.bars[0], symbol=Symbol.ETHUSDT),))
    with pytest.raises(ValueError, match="mixed"):
        replace(h, interval=Interval.DAY)
    for bars in (
        (h.bars[1], h.bars[0]),
        (h.bars[0], h.bars[0]),
        (h.bars[0], replace(h.bars[1], start=h.bars[1].start + timedelta(hours=1))),
    ):
        with pytest.raises(ValueError):
            replace(h, bars=bars, as_of=h.as_of + timedelta(hours=1))
    with pytest.raises(ValueError):
        replace(h, as_of=h.as_of.replace(tzinfo=None))


@pytest.mark.parametrize("calc,params", [(c.s1_a, A1), (c.s1_b, B1)])
@pytest.mark.parametrize("short", [False, True])
def test_s1_transition_daily_signal_and_monotonic_stop(calc, params, short):
    values = [100, 102, 104, 106] if not short else [100, 98, 96, 94]
    h = history(values, Interval.DAY)
    first = calc(h.through(h.bars[2].start), state(), params)
    assert first.state.trend is (Trend.SHORT if short else Trend.LONG)
    assert first.signal.kind == "ENTRY"
    assert first.signal.stop_price.value == (99 if short else 101)
    second = calc(h, first.state, params)
    assert second.signal.signal_id != first.signal.signal_id
    assert second.signal.direction is first.signal.direction
    assert second.state.stop_price.value == (97 if short else 103)
    # Retaining trend must never loosen an already better stop.
    improved = replace(first.state, stop_price=Price(D(96 if short else 104)))
    assert calc(h, improved, params).state.stop_price == improved.stop_price
    reverse = history(values + ([110] if short else [90]), Interval.DAY)
    switched = calc(reverse, second.state, params)
    assert switched.signal.kind == "ENTRY"
    assert switched.signal.direction is not second.signal.direction
    assert switched.state.stop_price.value == (93 if short else 107)


def test_s1_equality_inside_channel_and_warmup():
    prev = state(Trend.LONG, 90)
    assert c.s1_a(history([102, 102], Interval.DAY), prev, A1).state.trend is Trend.LONG
    h = history([100, 102, 101], Interval.DAY)
    assert c.s1_b(h, prev, B1).state.trend is Trend.LONG
    for calc, params in ((c.s1_a, A1), (c.s1_b, B1)):
        result = calc(history([100], Interval.DAY), prev, params)
        assert result.state.trend is Trend.FLAT and result.signal is None
    # Strict boundary: current close == prior high is not a breakout.
    assert c.s1_b(history([100, 102, 103], Interval.DAY), state(), B1).signal is None


@pytest.mark.parametrize(
    "calc,params,values", [(c.s2_a, A2, [100, 100, 90, 95]), (c.s2_b, B2, [100, 98, 96, 98])]
)
@pytest.mark.parametrize("short", [False, True])
def test_s2_real_reentry_direction_atr_and_no_unowned_exit(calc, params, values, short):
    values = [200 - v for v in values] if short else values
    h = history(values)
    trend = Trend.SHORT if short else Trend.LONG
    s1 = state(trend, 120 if short else 80)
    result = calc(h, s1, None, params)
    assert result.signal.kind == "ENTRY"
    assert result.signal.direction is (Direction.SHORT if short else Direction.LONG)
    expected = h.bars[-1].close + (1 if short else -1) * atr(h, params.atr_period)
    assert abs(result.signal.stop_price.value - expected) < D("1e-24")
    assert calc(h, state(), None, params).signal is None
    opposite = state(Trend.LONG if short else Trend.SHORT, 80 if short else 120)
    assert calc(h, opposite, None, params).signal is None
    # An already open episode cannot produce another ENTRY.
    episode = S2Episode(BTC, result.signal.direction, h.bars[-1].start, result.signal.stop_price)
    owned = calc(h, s1, episode, params)
    assert owned.signal is None or owned.signal.kind == "EXIT"
    # Price is at the mean/neutral without a crossing: no unowned EXIT.
    assert calc(history([100, 100, 100, 100]), s1, None, params).signal is None


@pytest.mark.parametrize("old_close,expected", [(95, False), (93, True)])
def test_bollinger_uses_each_bars_own_band(monkeypatch, old_close, expected):
    h = history([100, 100, old_close, 97])

    def bands(prefix, n, k):
        return Bands(D(100), D(106), D(94 if len(prefix.bars) == 3 else 96))

    monkeypatch.setattr(c, "bollinger", bands)
    result = c.s2_a(h, state(Trend.LONG, 80), None, A2)
    assert (result.signal is not None) == expected


@pytest.mark.parametrize("calc,params", [(c.s2_a, A2), (c.s2_b, B2)])
@pytest.mark.parametrize("short", [False, True])
def test_s2_exit_prices_mean_neutral_and_trend_change(calc, params, short):
    h = history([100, 100, 100, 100])
    direction = Direction.SHORT if short else Direction.LONG
    trend = Trend.SHORT if short else Trend.LONG
    episode = S2Episode(BTC, direction, START, Price(D(120 if short else 80)))
    result = calc(h, state(trend, 120 if short else 80), episode, params)
    assert result.signal.kind == "EXIT"
    assert result.signal.entry_price == Price(D(100))
    assert result.signal.stop_price == episode.stop_price
    assert result.signal.direction is direction
    # Trend loss/flip exits even without enough history for indicator or ATR.
    for s1 in (state(), state(Trend.LONG if short else Trend.SHORT, 80 if short else 120)):
        assert calc(history([100]), s1, episode, params).signal.kind == "EXIT"


@pytest.mark.parametrize("calc,params", [(c.s2_a, A2), (c.s2_b, B2)])
@pytest.mark.parametrize("short", [False, True])
def test_maximum_holding_boundary_includes_entry_bar(calc, params, short):
    h = history([100, 99, 98, 97] if not short else [100, 101, 102, 103])
    trend = Trend.SHORT if short else Trend.LONG
    s1 = state(trend, 120 if short else 80)
    episode = S2Episode(BTC, Direction(trend.value), START, Price(D(120 if short else 80)))
    assert calc(h, s1, episode, replace(params, max_hold_bars=4)).signal is None
    assert calc(h, s1, episode, replace(params, max_hold_bars=3)).signal.kind == "EXIT"


@pytest.mark.parametrize(
    "constructor,args",
    [
        (S1AParams, (2, 2, 1)),
        (S1AParams, (True, 3, 1)),
        (S1BParams, (0, 2)),
        (S2AParams, (3, D(0), 2, D(1), 10)),
        (S2AParams, (3, D(1), 2, D(-1), 10)),
        (S2BParams, (2, D(30), D(70), D(80), 2, D(1), 10)),
        (S2BParams, (2, D(30), D("NaN"), D(50), 2, D(1), 10)),
        (S2BParams, (2, D(30), D(70), D(50), 2, D(1), 0)),
    ],
)
def test_invalid_parameters(constructor, args):
    with pytest.raises((TypeError, ValueError)):
        constructor(*args)


def test_parameter_fields_have_no_defaults():
    for cls in (S1AParams, S1BParams, S2AParams, S2BParams):
        assert all(f.default is MISSING and f.default_factory is MISSING for f in fields(cls))


def test_state_and_outputs_json_roundtrip():
    h = history([100, 102, 104], Interval.DAY)
    result = c.s1_a(h, state(), A1)
    assert S1Result.from_dict(json.loads(json.dumps(result.to_dict()))) == result
    assert S1State.from_dict(json.loads(json.dumps(result.state.to_dict()))) == result.state
    assert S1State.from_dict(json.loads(json.dumps(state().to_dict()))) == state()
    h = history([100, 100, 90, 95])
    result2 = c.s2_a(h, state(Trend.LONG, 80), None, A2)
    episode = S2Episode(BTC, Direction.LONG, START, result2.signal.stop_price)
    assert S2Episode.from_dict(json.loads(json.dumps(episode.to_dict()))) == episode
    for output in (result2, S2Result(None), c.s2_a(h, state(), episode, A2)):
        assert S2Result.from_dict(json.loads(json.dumps(output.to_dict()))) == output
    assert c.s2_a(h, state(Trend.LONG, 80), None, A2) == result2


@pytest.mark.parametrize("calc,params", [(c.s1_a, A1), (c.s1_b, B1), (c.s2_a, A2), (c.s2_b, B2)])
def test_future_append_does_not_change_prefix_indicators_signals_or_stops(calc, params):
    daily = calc in (c.s1_a, c.s1_b)
    interval = Interval.DAY if daily else Interval.HOUR
    values = [100, 98, 96, 98, 100, 104, 105, 103, 101, 100, 102]
    full = history(values + [500, 5, 1000], interval)
    previous = state() if daily else state(Trend.LONG, 80)
    for i in range(1, len(values) + 1):
        prefix = history(values[:i], interval)
        sliced = full.through(prefix.bars[-1].start)
        for indicator in (sma, rsi, atr):
            assert indicator(prefix, 3) == indicator(sliced, 3)
        assert bollinger(prefix, 3, D(1)) == bollinger(sliced, 3, D(1))
        assert channel(prefix, 3, include_current=False) == channel(
            sliced, 3, include_current=False
        )
        args = (previous, params) if daily else (previous, None, params)
        actual = calc(prefix, *args)
        assert actual == calc(sliced, *args) == calc(prefix, *args)
        if daily:
            previous = actual.state


def test_state_time_and_contract_boundaries():
    h = history([100, 100, 90, 95])
    with pytest.raises(ValueError, match="future daily"):
        c.s2_a(h, S1State(BTC, Trend.LONG, Price(D(80)), START), None, A2)
    with pytest.raises(ValueError):
        c.s2_a(h, state(Trend.LONG, 80), S2Episode(BTC, Direction.LONG, h.as_of, Price(D(80))), A2)
    with pytest.raises(ValueError):
        c.s2_a(h, replace(state(), symbol=Symbol.ETHUSDT), None, A2)
    daily = history([100, 102, 104], Interval.DAY)
    result = c.s1_a(daily, state(), A1)
    with pytest.raises(ValueError, match="precede"):
        c.s1_a(daily, result.state, A1)


def test_s1_target_connects_to_portfolio_trail_stop():
    from test_portfolio import RISK_INPUT, setup, signal
    from test_risk import market

    from richletsgo.domain import ExecutionEvent, Qty

    async def scenario():
        db, clock, svc, router = setup()
        admitted = await router.handle(signal(), market(), RISK_INPUT)
        order = admitted.order
        event = ExecutionEvent(
            "strategy-entry",
            order.order_link_id,
            "venue-entry",
            BTC,
            Qty(D(1)),
            Price(D(100)),
            D(0),
            clock.now(),
            False,
        )
        await svc.on_execution(event)
        output = c.s1_a(history([96, 98, 100], Interval.DAY), state(), A1)
        assert output.state.stop_price == Price(D(97))
        async with db.unit_of_work() as u:
            svc.trail_stop(u, StrategyId.S1, BTC, market().instrument, output.state.stop_price)
            await u.commit()
        async with db.unit_of_work() as u:
            assert u.portfolio.book(StrategyId.S1, BTC).stop_price == output.state.stop_price

    asyncio.run(scenario())


@pytest.mark.parametrize("calc,params", [(c.s2_a, A2), (c.s2_b, B2)])
def test_s2_warmup_never_blocks_forced_exit(calc, params):
    h = history([100])
    assert calc(h, state(Trend.LONG, 80), None, params).signal is None
    episode = S2Episode(BTC, Direction.LONG, START - timedelta(hours=20), Price(D(80)))
    assert calc(h, state(Trend.LONG, 80), episode, params).signal.kind == "EXIT"


def test_empty_history_and_stop_channel_warmup():
    for calc, params in ((c.s1_a, A1), (c.s1_b, B1)):
        assert calc(history([], Interval.DAY), state(), params).signal is None
        params = replace(params, exit_channel=10)
        result = calc(history([100, 102, 104], Interval.DAY), state(), params)
        assert result.state.trend is Trend.FLAT and result.signal is None
    assert c.s2_a(history([]), state(), None, A2).signal is None
    assert c.s2_b(history([]), state(), None, B2).signal is None


def test_nonpositive_atr_stop_suppresses_entry():
    assert (
        c.s2_a(
            history([100, 100, 90, 95]),
            state(Trend.LONG, 80),
            None,
            replace(A2, atr_multiple=D(100)),
        ).signal
        is None
    )


@pytest.mark.parametrize("short", [False, True])
def test_rsi_threshold_equality_counts_return_but_not_previous_extreme(monkeypatch, short):
    h = history([100, 99, 98, 100])
    s1 = state(Trend.SHORT if short else Trend.LONG, 120 if short else 80)
    threshold = B2.high if short else B2.low
    for prior, expected in ((threshold, False), (threshold + (1 if short else -1), True)):

        def values(prefix, n, prior=prior):
            return prior if len(prefix.bars) == 3 else threshold

        monkeypatch.setattr(c, "rsi", values)
        assert (c.s2_b(h, s1, None, B2).signal is not None) == expected


@pytest.mark.parametrize("n", [1, 2, 14, 30])
def test_wilder_fixed_window_exact_equality_after_history_prepend(n):
    h = history([100 + (i * 7) % 19 for i in range(1000)])
    window = replace(h, bars=h.bars[-10 * n :])
    for indicator in (rsi, atr):
        assert indicator(h, n) == indicator(window, n)
        older = replace(h, bars=(replace(h.bars[0], close=D(999), high=D(1000)),) + h.bars[1:])
        assert indicator(older, n) == indicator(window, n)


@pytest.mark.parametrize("calc,params", [(c.s1_a, A1), (c.s1_b, B1), (c.s2_a, A2), (c.s2_b, B2)])
def test_bounded_candidate_window_matches_full_history(calc, params):
    daily = calc in (c.s1_a, c.s1_b)
    h = history([100 + (i * 7) % 19 for i in range(1000)], Interval.DAY if daily else Interval.HOUR)
    prev = state(Trend.LONG, 80)
    args = (prev, params) if daily else (prev, None, params)
    for index in (0, 1, 20, 50, 999):
        prefix = h.through(h.bars[index].start)
        bounded = h.through(h.bars[index].start, lookback=c.required_bars(params))
        assert len(bounded.bars) <= c.required_bars(params)
        assert calc(prefix, *args) == calc(bounded, *args)


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("equal", [False, True])
def test_s1_b_breached_channel_exits_without_amending_target(short, equal):
    # Prior five-day low=99, prior three-day low=104: no reversal on day 9,
    # but the exit-channel candidate (104) is above/equal to today's close.
    closes = [100, 102, 104, 106, 100, 105, 107, 106, 104 if equal else 102]
    if short:
        closes = [220 - v for v in closes]
    h = history(closes, Interval.DAY)
    trend = Trend.SHORT if short else Trend.LONG
    prev = S1State(BTC, trend, Price(D(120 if short else 100)), h.bars[-2].start)
    output = c.s1_b(h, prev, S1BParams(5, 3))
    assert output.state.trend is trend
    assert output.state.stop_price == prev.stop_price
    assert output.signal.kind == "EXIT"
    assert output.signal.entry_price.value == h.bars[-1].close
    assert output.signal.stop_price == prev.stop_price
    assert output.signal.direction is Direction(trend.value)
    assert S1Result.from_dict(json.loads(json.dumps(output.to_dict()))) == output
    # Once the channel is safe again, the following daily market signal resumes.
    following = history(closes + [110 if not short else 110], Interval.DAY)
    assert c.s1_b(following, output.state, S1BParams(5, 3)).signal.kind == "ENTRY"


@pytest.mark.parametrize("short", [False, True])
def test_s1_a_breached_channel_exit_and_missing_previous_target(short):
    h = history([100, 110, 110, 105] if not short else [120, 110, 110, 115], Interval.DAY)
    # SMA(3) remains above/below SMA(4), exit channel is already crossed.
    params = S1AParams(3, 4, 2)
    trend = Trend.SHORT if short else Trend.LONG
    for previous in (state(), state(trend, 120 if short else 100)):
        output = c.s1_a(h, previous, params)
        assert output.state.trend is trend and output.signal.kind == "EXIT"
        assert output.state.stop_price == previous.stop_price
        assert S1Result.from_dict(json.loads(json.dumps(output.to_dict()))) == output
