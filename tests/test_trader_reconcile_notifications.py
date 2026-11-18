"""Synthetic interleavings and notification sampling; no service/process access."""

import asyncio
from datetime import timedelta

import pytest
from test_m304_trader import SYMBOLS, T, candle, runtime

from richletsgo.config import TelegramConfig
from richletsgo.marketdata.models import Interval
from richletsgo.notify.telegram import Telegram
from richletsgo.risk.pause import Pause, PauseKind


@pytest.mark.parametrize("restart", [False, True])
def test_midnight_maintenance_between_funding_and_account_publish_does_not_rearm_wait(
    tmp_path, restart
):
    async def run():
        db, clock, trader, inputs, sync, alerts = await runtime(tmp_path)
        entered, release, ticked = asyncio.Event(), asyncio.Event(), asyncio.Event()
        tasks = []
        at = T
        if restart:
            from richletsgo.risk.equity import EquityTracker

            clock.set(T + timedelta(hours=4))
            at = clock.now()
            trader.initialize_tracker = True
            trader.gate.tracker.publish(EquityTracker.unconfirmed(T - timedelta(days=7)))
        try:
            assert await trader.boot()
            for interval in (Interval.HOUR,) if restart else (Interval.HOUR, Interval.DAY):
                for symbol in SYMBOLS:
                    await trader.accept(candle(symbol, interval, at))
            original_account, original_tick = inputs.account, trader.control.tick

            async def account():
                entered.set()
                await release.wait()
                return await original_account()

            async def tick():
                await original_tick()
                ticked.set()

            inputs.account, trader.control.tick = account, tick
            tasks.append(asyncio.create_task(trader.process_next()))
            await asyncio.wait_for(entered.wait(), 5)
            assert not trader.funding_wait  # Funding stage has completed.
            assert trader.gate.tracker.day_start < T and not trader.can_enter()
            tasks.append(asyncio.create_task(trader.maintenance()))
            await asyncio.wait_for(ticked.wait(), 5)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert trader.gate.tracker.day_start == T
            assert trader.can_enter() and not trader.funding_wait
            for _ in range(5):
                clock.advance(timedelta(minutes=1))
                await trader.maintenance()
                assert trader.can_enter() and not trader.funding_wait
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await trader.close()

    asyncio.run(run())


async def notifier(db, clock, trader, sync):
    from test_telegram import FakeBot

    bot = FakeBot()

    async def sleep(_):
        pass

    tg = Telegram(
        database=db,
        service=trader.control,
        bot=bot,
        clock=clock,
        policy=TelegramConfig(20, 30, 1, 300, 2, 100, 1, 120),
        users=(1,),
        expires_at=T + timedelta(days=30),
        warning_window=timedelta(minutes=1),
        sleep=sleep,
        pause_visible=sync.pause_visible,
    )
    await tg.initialize()
    return tg, bot


def test_25_normal_comparisons_block_entries_but_emit_no_sync_notifications(tmp_path):
    from test_m304_trader import virtual_runtime

    from richletsgo.domain import StrategyId

    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        tg, bot = await notifier(db, clock, trader, sync)
        entered, release = asyncio.Event(), asyncio.Event()
        task = None
        try:
            assert await trader.boot()
            original = exchange.get_positions

            async def positions():
                entered.set()
                await release.wait()
                return await original()

            exchange.get_positions = positions
            sync.alerts.clear()
            for _ in range(25):
                entered.clear()
                release.clear()
                task = asyncio.create_task(sync.synchronize())
                await asyncio.wait_for(entered.wait(), 5)
                assert trader.gate.pauses.blocking(StrategyId.S1, SYMBOLS[0])
                assert not sync.pause_visible(
                    next(p for p in sync.pauses.active() if p.reason == "sync")
                )
                await tg.scheduled_once()
                await tg.send_once()
                release.set()
                assert await asyncio.wait_for(task, 5)
                await tg.scheduled_once()
                await tg.send_once()
                assert not sync.alerts and not bot.sent
                clock.advance(timedelta(minutes=1))
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await trader.close()

    asyncio.run(run())


@pytest.mark.parametrize("failed", [False, True])
def test_persistent_or_failed_sync_is_visible_and_entry_pause_remains(tmp_path, failed):
    from test_m304_trader import virtual_runtime

    from richletsgo.domain import StrategyId
    from richletsgo.exchange.contracts import ErrorCode, ExchangeError

    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        tg, bot = await notifier(db, clock, trader, sync)
        entered, release = asyncio.Event(), asyncio.Event()
        task = None
        try:
            assert await trader.boot()
            original = exchange.get_positions

            async def positions():
                entered.set()
                await release.wait()
                return (
                    ExchangeError(ErrorCode.QUERY_FAILED, "synthetic")
                    if failed
                    else await original()
                )

            exchange.get_positions = positions
            task = asyncio.create_task(sync.synchronize())
            await asyncio.wait_for(entered.wait(), 5)
            if not failed:
                clock.advance(sync.policy.unknown_alert_after + timedelta(seconds=1))
                await tg.scheduled_once()
                await tg.send_once()
                assert any("sync" in m["text"] for m in bot.sent)
            release.set()
            assert await asyncio.wait_for(task, 5) is (not failed)
            if failed:
                assert trader.gate.pauses.blocking(StrategyId.S1, SYMBOLS[0])
                assert any(getattr(n, "code", None) == "sync_unstable" for n in sync.alerts)
                await tg.scheduled_once()
                await tg.send_once()
                assert any("sync" in m["text"] for m in bot.sent)
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await trader.close()

    asyncio.run(run())


def test_pause_on_off_transitions_share_one_window_and_latest_state(tmp_path):
    from test_telegram import runtime as telegram_runtime

    async def run():
        db, service, clock, bot, tg, _ = await telegram_runtime(tmp_path)
        for _ in range(10):
            service.pauses.add(Pause(PauseKind.ENTRY_PAUSED_SYSTEM, "stream", clock.now()))
            await tg.scheduled_once()
            await tg.send_once()
            service.pauses.clear(PauseKind.ENTRY_PAUSED_SYSTEM, reason="stream")
            await tg.scheduled_once()
            await tg.send_once()
            clock.advance(timedelta(seconds=1))
        assert len(bot.sent) == 1 and "stream" in bot.sent[0]["text"]
        clock.advance(timedelta(minutes=1))
        await tg.scheduled_once()
        await tg.send_once()
        assert len(bot.sent) == 2 and "1분 동안 19번" in bot.sent[-1]["text"]
        assert "멈춤 원인 해결" in bot.sent[-1]["text"]
        assert not service.pauses.active()
        service.pauses.add(Pause(PauseKind.HALTED, "drawdown", clock.now()))
        await tg.scheduled_once()
        await tg.send_once()
        assert len(bot.sent) == 3 and "drawdown" in bot.sent[-1]["text"]

    asyncio.run(run())


def test_pause_batch_survives_notifier_restart_and_full_delivery_queue(tmp_path):
    from dataclasses import replace

    from test_telegram import runtime as telegram_runtime

    async def run():
        db, service, clock, bot, tg, _ = await telegram_runtime(tmp_path)
        tg.policy = replace(tg.policy, queue_limit=1)
        service.pauses.add(Pause(PauseKind.ENTRY_PAUSED_SYSTEM, "stream", clock.now()))
        await tg.scheduled_once()  # First transition fills the single delivery slot.
        service.pauses.clear(PauseKind.ENTRY_PAUSED_SYSTEM, reason="stream")
        await tg.scheduled_once()  # Suppression records the latest off state durably.
        clock.advance(timedelta(minutes=1))
        await tg.scheduled_once()  # Summary cannot fit; do not consume its batch state.
        await tg.send_once()
        assert len(bot.sent) == 1
        restarted = Telegram(
            database=db,
            service=service,
            bot=bot,
            clock=clock,
            policy=tg.policy,
            users=tg.users,
            expires_at=tg.expires_at,
            warning_window=tg.warning_window,
            sleep=tg.sleep,
        )
        await restarted.initialize()
        await restarted.scheduled_once()
        await restarted.send_once()
        assert len(bot.sent) == 2
        assert "멈춤 원인 해결" in bot.sent[-1]["text"]
        assert "1분 동안 1번" in bot.sent[-1]["text"]
        await restarted.scheduled_once()
        await restarted.send_once()
        assert len(bot.sent) == 2

    asyncio.run(run())
