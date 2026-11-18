"""Restart regressions: real Bybit range predicate, synthetic clock and exchange only."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from test_m304_trader import SYMBOLS, T, virtual_runtime
from test_trader_reconcile_notifications import notifier

from richletsgo.exchange.bybit.gateway import BybitGateway
from richletsgo.exchange.contracts import ErrorCode, ExchangeError
from richletsgo.risk.equity import AccountSnapshot, EquityTracker
from richletsgo.risk.pause import Pause, PauseKind


def test_seven_hour_restart_replays_without_invalid_history_request(tmp_path):
    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        sync.policy = replace(sync.policy, history_window=timedelta(days=6, hours=23))
        last = T + timedelta(hours=5)
        clock.set(T + timedelta(hours=12, minutes=16, seconds=17))
        limits = SimpleNamespace(
            clock=clock,
            config=SimpleNamespace(page_limit=50),
            _bad=lambda reason: ExchangeError(ErrorCode.QUERY_FAILED, reason),
        )
        seen = set()

        def watch(owner, name):
            original = getattr(owner, name)

            async def checked(*args):
                assert isinstance(BybitGateway._range(limits, args[-1]), dict), (name, args[-1])
                seen.add(name)
                return await original(*args)

            setattr(owner, name, checked)

        for method in ("get_order_history", "get_executions"):
            watch(exchange, method)
        for method in ("funding", "cash_flows"):
            watch(inputs, method)
        async with db.unit_of_work() as u:
            u.recovery.put("trader:last_boundary", last.isoformat())
            for symbol in SYMBOLS:
                u.recovery.put("reconciliation:" + symbol.value, last.isoformat())
            await u.commit()
        try:
            # The old caller's lower bound is rejected by the actual adapter predicate.
            assert isinstance(
                BybitGateway._range(limits, last + timedelta(hours=1) - sync.policy.history_window),
                ExchangeError,
            )
            assert await trader.boot()
            trader.gate.tracker.publish(
                EquityTracker.open(AccountSnapshot(last, Decimal(1000), Decimal(0)))
            )
            sync.pauses.add(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "manual", last))
            await trader.maintenance()
            for hour in range(6, 13):
                assert await trader.process_next()
                assert trader.last_boundary == T + timedelta(hours=hour)
            assert seen == {"get_order_history", "get_executions", "cash_flows", "funding"}
        finally:
            await trader.close()

    asyncio.run(run())


def test_old_boundary_with_recent_watermark_leaves_durable_gap_and_future_waits(tmp_path):
    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        try:
            assert await trader.boot()
            future = clock.now() + timedelta(hours=1)
            assert not await trader.process(future)
            async with db.unit_of_work() as u:
                for symbol in SYMBOLS:
                    u.recovery.put("reconciliation:" + symbol.value, future.isoformat())
                await u.commit()
            assert await sync._since() == clock.now() - sync.policy.history_window
            trader.last_boundary = T - timedelta(days=8)
            assert not await trader.process(trader.last_boundary + timedelta(hours=1))
            assert not await sync.synchronize()
            async with db.unit_of_work() as u:
                assert all(
                    u.recovery.get("history_gap:" + s.value) == "unresolved" for s in SYMBOLS
                )
                assert u.recovery.get("trader:last_boundary") is None
            assert any(p.reason == "history_out_of_range" for p in sync.pauses.active())
        finally:
            await trader.close()

    asyncio.run(run())


def test_failed_boundary_query_not_cleared_by_shorter_successful_comparison(tmp_path):
    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        tg, bot = await notifier(db, clock, trader, sync)
        try:
            assert await trader.boot()
            original = exchange.get_executions
            since = clock.now() - timedelta(days=5)

            async def executions(symbol, start):
                if start == since:
                    return ExchangeError(ErrorCode.QUERY_FAILED, "synthetic")
                return await original(symbol, start)

            exchange.get_executions = executions
            sync.alerts.clear()
            notices = []
            for _ in range(4):
                assert not await sync.confirm_fills(since)
                assert await sync.synchronize()
                assert any(p.reason == "boundary_query" for p in sync.pauses.active())
                notices.extend(sync.alerts)
                sync.alerts.clear()
                await tg.scheduled_once()
                await tg.send_once()
                clock.advance(timedelta(minutes=1))
            assert notices == ["boundary_query"]
            assert len(bot.sent) == 1
            exchange.get_executions = original
            assert await sync.confirm_fills(since)
            await tg.scheduled_once()
            await tg.send_once()
            assert len(bot.sent) == 2 and "정지 해소" in bot.sent[-1]["text"]
        finally:
            await trader.close()

    asyncio.run(run())


def test_cash_flow_watermark_ahead_of_clock_is_deferred_then_observed(tmp_path):
    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        try:
            assert await trader.boot()
            ahead = T + timedelta(seconds=2)
            trader.gate.tracker.publish(
                EquityTracker.open(AccountSnapshot(ahead, Decimal(1000), Decimal(0)))
            )
            calls = []

            async def flows(since):
                assert since <= clock.now()
                calls.append(since)
                return ()

            inputs.cash_flows = flows
            await trader.maintenance()
            assert not calls and trader.gate.tracker.state.latest.at == ahead
            clock.advance(timedelta(minutes=1))
            await trader.maintenance()
            assert calls == [ahead] and trader.gate.tracker.state.latest.at == clock.now()
        finally:
            await trader.close()

    asyncio.run(run())


def test_stale_cash_flow_baseline_cannot_complete_boundary_with_partial_history(tmp_path):
    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        try:
            assert await trader.boot()
            old = T - timedelta(days=8)
            trader.gate.tracker.publish(
                EquityTracker.open(AccountSnapshot(old, Decimal(1000), Decimal(0)))
            )
            at = T + timedelta(hours=1)
            clock.set(at + timedelta(minutes=2))

            async def unexpected(_):
                raise AssertionError("must not query a partial cash flow interval")

            inputs.cash_flows = unexpected
            assert not await trader.process(at)
            async with db.unit_of_work() as u:
                assert all(u.recovery.get("history_gap:" + s.value) for s in SYMBOLS)
                assert u.recovery.get("boundary_checkpoint:" + at.isoformat() + ":account") is None
                assert u.recovery.get("trader:last_boundary") is None
        finally:
            await trader.close()

    asyncio.run(run())
