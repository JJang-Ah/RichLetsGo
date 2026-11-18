"""Bybit live input mapping and risk paths using constructed REST responses only."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from test_bybit_adapter import position_row
from test_m304_trader import SYMBOLS, T, candle, runtime
from test_service_config import config_file

from richletsgo.config import load_config
from richletsgo.domain import PositionSnapshot, Price, Qty, StrategyId
from richletsgo.exchange.bybit.mapping import position
from richletsgo.live.inputs import BybitInputs
from richletsgo.marketdata.models import Interval, to_ms
from richletsgo.portfolio.models import StrategyBook
from richletsgo.risk.pause import PauseState
from richletsgo.storage.codec import decode, encode
from richletsgo.strategy.boundary import boundary_signals


def test_account_funding_sign_bounds_and_all_pages_are_requested():
    async def run():
        async def pages(path, params):
            assert path == "/v5/account/transaction-log"
            assert params["type"] == "SETTLEMENT" and params["currency"] == "USDT"
            return [
                dict(
                    currency="USDT",
                    type="SETTLEMENT",
                    symbol=s.value,
                    id=str(i),
                    transactionTime=to_ms(T),
                    funding=value,
                )
                for i, (s, value) in enumerate(zip(SYMBOLS, (".2", "-.3"), strict=True))
            ]

        gateway = SimpleNamespace(
            _range=lambda since: {"startTime": to_ms(since), "endTime": to_ms(T)},
            rest=SimpleNamespace(pages=pages),
        )
        inputs = BybitInputs(SimpleNamespace(bybit=SimpleNamespace(symbols=SYMBOLS)), gateway, None)
        payments = await inputs.funding(T - timedelta(hours=1))
        assert [p.cost for p in payments] == [D("-.2"), D(".3")]
        assert all(p.settled_at == T for p in payments)

    asyncio.run(run())


def test_bounded_rest_candles_and_unfinished_bar_rejection():
    async def run():
        invalid = False

        async def call(method, path, params, **kwargs):
            assert kwargs["public"] and path == "/v5/market/kline"
            assert params["start"] == to_ms(T - timedelta(hours=1))
            assert params["end"] == to_ms(T) - 1
            start = T if invalid else T - timedelta(hours=1)
            return {
                "result": {"list": [[str(to_ms(start)), "100", "101", "99", "100", "1", "100"]]}
            }

        inputs = BybitInputs(None, SimpleNamespace(rest=SimpleNamespace(call=call)), None)
        assert await inputs.bar(SYMBOLS[0], Interval.HOUR, T) == candle(SYMBOLS[0], Interval.HOUR)
        invalid = True
        with pytest.raises(ValueError, match="outside"):
            await inputs.bar(SYMBOLS[0], Interval.HOUR, T)

    asyncio.run(run())


def test_actual_liquidation_price_maps_and_unsafe_book_requests_reduction(tmp_path):
    assert position(
        position_row(size="1", side="Buy", avgPrice="100", liqPrice="94"), T
    ).liquidation_price == Price(D(94))

    async def run():
        db, clock, trader, _, sync, _ = await runtime(tmp_path)
        alerts = []

        async def positions():
            return (PositionSnapshot(SYMBOLS[0], Qty(D(1)), Price(D(100)), T, Price(D(94))),)

        async with db.unit_of_work() as u:
            u.portfolio.put_book(
                StrategyBook(StrategyId.S1, SYMBOLS[0], D(1), D(100), stop_price=Price(D(95)))
            )
            from richletsgo.risk.records import Direction, EpisodeRecord, EpisodeState

            u.episodes.put(
                EpisodeRecord(
                    "unsafe-liq",
                    StrategyId.S1,
                    SYMBOLS[0],
                    Direction.LONG,
                    Qty(D(1)),
                    T,
                    ("entry",),
                    EpisodeState.OPEN,
                )
            )
            await u.commit()
        portfolio = trader.portfolio
        gateway = SimpleNamespace(
            get_positions=positions, clock=clock, pauses=trader.gate.pauses, pause=alerts.append
        )
        inputs = BybitInputs(
            SimpleNamespace(risk=trader.gate.limits),
            gateway,
            SimpleNamespace(gate=trader.gate, portfolio=portfolio),
        )
        await inputs.check_liquidation()
        assert alerts == ["actual_liquidation_buffer"]
        async with db.unit_of_work() as u:
            assert u.portfolio.intent("unsafe-liq") is not None
            assert u.episodes.active_for(StrategyId.S1, SYMBOLS[0]).state.value == "EXITING"

    asyncio.run(run())


def test_optional_s2_emits_no_signals_and_common_strategy_order(tmp_path):
    async def run():
        seen = []

        async def first(s):
            seen.append(("S1", s))

        async def second(s):
            seen.append(("S2", s))

        async def settle():
            seen.append("settle")

        await boundary_signals(T, SYMBOLS, object(), first, second, settle)
        assert seen == [("S1", s) for s in SYMBOLS] + [("S2", s) for s in SYMBOLS] + ["settle"]
        seen.clear()
        await boundary_signals(T, SYMBOLS, None, first, second, settle)
        assert seen == [("S1", s) for s in SYMBOLS]

    asyncio.run(run())


def test_live_signal_serialization_and_history_gap_is_per_symbol(tmp_path):
    from test_portfolio import signal

    assert decode(encode(signal())) == signal()

    async def run():
        db, clock, trader, _, _, alerts = await runtime(tmp_path)
        gateway = SimpleNamespace(clock=clock, pauses=PauseState(), alert=alerts.append)
        config = load_config(config_file(tmp_path))
        inputs = BybitInputs(
            replace(config, strategies={StrategyId.S1: config.strategies[StrategyId.S1]}),
            gateway,
            SimpleNamespace(),
        )
        async with db.unit_of_work() as u:
            assert await inputs.signals(u, T, (), frozenset(SYMBOLS)) == ()
        assert len(gateway.pauses.active()) == 2

    asyncio.run(run())


def test_restart_across_midnight_recovers_only_complete_symbol_history(tmp_path):
    from richletsgo.strategy import candidates

    async def run():
        db, clock, trader, stub, _, alerts = await runtime(tmp_path)
        gateway = SimpleNamespace(clock=clock, pauses=trader.gate.pauses, alert=alerts.append)
        config = load_config(config_file(tmp_path))
        inputs = BybitInputs(
            replace(config, strategies={StrategyId.S1: config.strategies[StrategyId.S1]}),
            gateway,
            None,
        )
        last = T - timedelta(hours=19)
        first = last + timedelta(hours=1)
        clock.set(T + timedelta(hours=12))
        async with db.unit_of_work() as u:
            u.recovery.put("trader:last_boundary", last.isoformat())
            await u.commit()

        async def fetch(symbol, interval, start, end):
            rows = tuple(
                candle(symbol, interval, start + interval.duration * (i + 1))
                for i in range((end - start) // interval.duration)
            )
            inputs.remember(rows)
            return rows

        async def maintain():
            pass

        inputs.fetch, inputs.maintain = fetch, maintain
        stub.warmup = inputs.warmup
        assert await trader.boot()
        count = candidates.required_bars(config.strategies[StrategyId.S1].params)
        cache = inputs.history[SYMBOLS[0], Interval.DAY]
        # Previous warmup had only this many bars relative to the restart.
        old = sorted(cache.values(), key=lambda b: b.start)[-count:]
        assert len([b for b in old if b.end <= first]) < count
        assert len(inputs.closed(SYMBOLS[0], Interval.DAY, first, count).bars) == count
        async with db.unit_of_work() as u:
            await inputs.signals(u, first, (), frozenset(SYMBOLS))
            assert not gateway.pauses.active()
            removed = []
            for symbol in SYMBOLS:
                series = inputs.history[symbol, Interval.DAY]
                removed.append(series.pop(Interval.DAY.floor(first) - Interval.DAY.duration))
            for _ in range(2):
                await inputs.signals(u, first, (), frozenset(SYMBOLS))
            assert not alerts and len(gateway.pauses.active()) == 2
            revision = gateway.pauses.revision
            inputs.remember((removed[0],))
            for _ in range(2):
                await inputs.signals(u, first + timedelta(hours=1), (), frozenset(SYMBOLS))
            assert [p.symbol for p in gateway.pauses.active()] == [SYMBOLS[1]]
            assert not alerts and gateway.pauses.revision == revision + 1
        await trader.close()

    asyncio.run(run())
