"""PR72 carry-over: synthetic clocks, archives and real boundary interleavings only."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_m304_trader import SYMBOLS, T, candle, runtime, virtual_runtime
from test_oms import order

from richletsgo.domain import Environment, OrderState, StrategyId, Symbol
from richletsgo.domain.clock import ManualClock
from richletsgo.live.trader import FundingPayment
from richletsgo.marketdata.models import Interval
from richletsgo.notify.batching import WarningBatcher
from richletsgo.portfolio.funding import FundingTimeInvalid, allocate, position_snapshot
from richletsgo.portfolio.models import ProtectionRecovery, StrategyBook
from richletsgo.storage.errors import StorageError
from richletsgo.storage.memory import InMemoryDatabase, InMemoryUnitOfWork
from richletsgo.storage.sqlite import SqliteDatabase, open_readonly


def test_warning_windows_latest_values_resolution_and_idle_cleanup():
    clock, notices = ManualClock(T), []
    batch = WarningBatcher(clock, timedelta(seconds=60), notices.append)
    for i in range(60):
        batch.warn(("clock", "BTC"), f"offset {i}")
        clock.advance(timedelta(seconds=1))
    assert notices == ["offset 0"]
    batch.flush()
    assert len(notices) == 2 and "offset 59 (repeated 59;" in notices[-1]
    batch.warn(("clock", "ETH"), "other")
    batch.warn(("clock", "BTC"), "offset 60")
    batch.resolve(("clock", "BTC"), "recovered")
    assert "repeated 1;" in notices[-2] and notices[-1] == "recovered"
    batch.warn(("clock", "BTC"), "new episode")
    assert notices[-1] == "new episode"
    clock.advance(timedelta(minutes=2))
    batch.flush()
    assert not batch._episodes
    with pytest.raises(ValueError):
        WarningBatcher(clock, timedelta(0), notices.append)


def test_warning_sink_can_be_called_from_sqlite_worker_threads():
    from concurrent.futures import ThreadPoolExecutor

    clock, notices = ManualClock(T), []
    batch = WarningBatcher(clock, timedelta(seconds=60), notices.append)
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: batch.warn("same cause", "warning"), range(200)))
    assert notices == ["warning"]
    clock.advance(timedelta(seconds=60))
    batch.flush()
    assert len(notices) == 2 and "repeated 199;" in notices[1]


@pytest.mark.parametrize("delta", [timedelta(seconds=1), timedelta(microseconds=1)])
def test_off_hour_funding_keeps_raw_time_and_midnight_wait(tmp_path, delta):
    async def run():
        db, clock, trader, inputs, sync, alerts = await runtime(tmp_path)
        assert await trader.boot()
        async with db.unit_of_work() as u:
            u.portfolio.put_book(StrategyBook(StrategyId.S1, SYMBOLS[0], D(1), D(100)))
            position_snapshot(u, T, SYMBOLS[0])
            with pytest.raises(FundingTimeInvalid):
                allocate(u, "bad", SYMBOLS[0], D(1), T + delta)
            await u.commit()
        for interval in (Interval.HOUR, Interval.DAY):
            for symbol in SYMBOLS:
                await trader.accept(candle(symbol, interval))
        inputs.payments = (FundingPayment("bad", SYMBOLS[0], T + delta, D(1)),)
        assert not await trader.process(T)
        assert trader.funding_wait
        async with db.unit_of_work() as u:
            assert not u.portfolio.funding_seen("bad")
            assert u.recovery.get("funding_received:" + T.isoformat() + ":BTCUSDT") is None
            assert (
                json.loads(u.recovery.get("pending_funding:bad"))["at"] == (T + delta).isoformat()
            )
        ticks = sync.ticks
        await trader.maintenance()
        assert sync.ticks > ticks and any("off-hour" in a for a in alerts)
        inputs.payments = (FundingPayment("good", SYMBOLS[0], T, D(1)),)
        assert await trader.process(T)
        async with db.unit_of_work() as u:
            assert u.portfolio.funding_seen("good")
            assert u.portfolio.book(StrategyId.S1, SYMBOLS[0]).funding == 1
        await trader.close()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "prefix",
    ["config_version", "boundary_input", "boundary_output", "position_snapshot", "funding_record"],
)
def test_immutable_replay_conflict_rolls_back_siblings(tmp_path, backend, prefix):
    async def run():
        db = (
            SqliteDatabase(tmp_path / "db.sqlite", "a", Environment.DEMO, now=lambda: T)
            if backend == "sqlite"
            else InMemoryDatabase("a", Environment.DEMO)
        )
        if backend == "sqlite":
            await db.migrate()
        key = prefix + ":test"
        async with db.unit_of_work() as u:
            u.recovery.put(key, json.dumps("original"))
            u.recovery.archive(key)
            await u.commit()
        async with db.unit_of_work() as u:
            u.recovery.put(key, json.dumps("original"))
            await u.commit()
        with pytest.raises(StorageError, match="immutable"):
            async with db.unit_of_work() as u:
                u.recovery.put("sibling", "must roll back")
                u.recovery.put(key, json.dumps("changed"))
                await u.commit()
        async with db.unit_of_work() as u:
            assert u.recovery.get(key) == json.dumps("original")
            assert u.recovery.get("sibling") is None
            u.recovery.put("boundary_checkpoint:test", "true")
            u.recovery.put("boundary_checkpoint:test", "false")
            await u.commit()

    asyncio.run(run())


def test_failed_storage_recovery_keeps_boundary_queued_and_preserves_new_pause(tmp_path):
    from richletsgo.risk.equity import AccountSnapshot, EquityTracker
    from richletsgo.risk.pause import Pause, PauseKind

    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        try:
            assert await trader.boot()
            trader.gate.tracker.publish(EquityTracker.open(AccountSnapshot(T, D(1000), D(0))))
            for interval in (Interval.HOUR, Interval.DAY):
                for symbol in SYMBOLS:
                    await trader.accept(candle(symbol, interval))
            trader._pending_txn = "marker"
            trader._last_rest = T

            async def fail():
                raise OSError("synthetic reconciliation failure")

            trader.recover = fail
            with pytest.raises(OSError):
                await trader.maintenance()
            assert not trader.ready and not await trader.process_next()
            assert trader._pending_txn == "marker" and T in trader._boundaries
            async with db.unit_of_work() as u:
                assert not u.decisions.all()

            async def recover():
                trader.gate.pauses.add(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "operator", T))

            trader.recover = recover
            clock.advance(trader.policy.tick)
            await trader.maintenance()
            assert trader.ready and trader._pending_txn is None
            assert any(p.reason == "operator" for p in trader.gate.pauses.active())
            # Recovery must not remove an operator pause to make the boundary admissible.
            assert await trader.process_next()
            async with db.unit_of_work() as u:
                assert len(u.decisions.all()) == 4
                assert all(d.kind.value != "ADMITTED" for d in u.decisions.all())
        finally:
            await trader.close()

    asyncio.run(run())


def test_immutable_projection_conflict_rolls_back_canonical_write(tmp_path):
    async def run():
        db = SqliteDatabase(tmp_path / "db.sqlite", "a", Environment.DEMO, now=lambda: T)
        await db.migrate()
        # An audit projection already present must also be checked, not blindly upserted.
        async with db.unit_of_work() as u:
            await db._submit(
                lambda: u._connection.execute(
                    "INSERT INTO boundary_inputs VALUES ('a','boundary_input:test','\"original\"',123)"
                )
            )
            await u.commit()
        with pytest.raises(StorageError, match="immutable"):
            async with db.unit_of_work() as u:
                u.recovery.put("boundary_input:test", json.dumps("changed"))
                await u.commit()
        async with db.unit_of_work() as u:
            assert u.recovery.get("boundary_input:test") is None
            u.recovery.put("boundary_input:test", json.dumps("original"))
            await u.commit()
        c = open_readonly(db.path)
        try:
            assert c.execute("SELECT payload,created_at FROM boundary_inputs").fetchone() == (
                json.dumps("original"),
                123,
            )
        finally:
            c.close()

    asyncio.run(run())


def test_recovery_only_decodes_matching_archived_orders(tmp_path, monkeypatch):
    from richletsgo.storage import sqlite as adapter

    async def run():
        db = SqliteDatabase(tmp_path / "db.sqlite", "a", Environment.DEMO, now=lambda: T)
        await db.migrate()
        old = T - timedelta(days=40)
        matching = replace(order(state=OrderState.FILLED), created_at=old, updated_at=old)
        async with db.unit_of_work() as u:
            u.orders.put(matching)
            for i in range(300):
                u.orders.put(
                    replace(
                        order(
                            strategy=StrategyId.S2,
                            state=OrderState.FILLED,
                            symbol=Symbol.ETHUSDT,
                            suffix=str(i),
                        ),
                        created_at=old,
                        updated_at=old,
                    )
                )
            u.portfolio.put_recovery(StrategyId.S1, Symbol.BTCUSDT, ProtectionRecovery("r", 0, T))
            await u.commit()
        decoded = []
        original = adapter.decode

        def decode(raw):
            value = original(raw)
            if type(value).__name__ == "OrderRecord":
                decoded.append(value.key)
            return value

        monkeypatch.setattr(adapter, "decode", decode)
        async with db.unit_of_work() as u:
            assert u.orders.get(matching.key) == matching
            assert len(u.orders.all()) == 1
        assert decoded == [matching.key]

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["transaction", "entry", "entry_paused"])
@pytest.mark.parametrize("first", ["recovery", "boundary"])
@pytest.mark.parametrize("committed", [False, True])
def test_storage_recovery_and_boundary_keep_signals(tmp_path, mode, first, committed):
    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        entered, routing, release, called = (asyncio.Event() for _ in range(4))
        tasks = []
        try:
            assert await trader.boot()
            from richletsgo.risk.equity import AccountSnapshot, EquityTracker

            trader.gate.tracker.publish(EquityTracker.open(AccountSnapshot(T, D(1000), D(0))))
            for interval in (Interval.HOUR, Interval.DAY):
                for symbol in SYMBOLS:
                    await trader.accept(candle(symbol, interval))
            original_route = inputs.route

            async def route(*args):
                routing.set()
                if first == "boundary":
                    await release.wait()
                return await original_route(*args)

            async def recover():
                entered.set()
                if first == "recovery":
                    await release.wait()
                db.entry_paused = False

            async def resolve(txn):
                entered.set()
                if first == "recovery":
                    await release.wait()
                # Marker resolution never holds the shared account lock.
                async with trader.gate.account_lock:
                    pass
                return committed

            async def resume(txn, resolver):
                sync.sender.pending_entries.pop(txn, None)

            async def maintenance():
                called.set()
                await trader.maintenance()

            inputs.route, trader.recover, trader.resolve_commit = route, recover, resolve
            sync.sender.resume_entry_commit = resume
            # Avoid a second, independent periodic REST comparison in this test.
            trader._last_rest = clock.now()
            if mode == "transaction":
                trader._pending_txn = "marker"
            elif mode == "entry":
                sync.sender.pending_entries["marker"] = order()
            else:
                db.entry_paused = True

                class RecoveryUow(InMemoryUnitOfWork):
                    @property
                    def entry_paused(self):
                        return db.entry_paused

                trader.uow = lambda: RecoveryUow(db)
            if first == "recovery":
                tasks.append(asyncio.create_task(maintenance()))
                await asyncio.wait_for(entered.wait(), 10)
                tasks.append(asyncio.create_task(trader.process_next()))
                await asyncio.sleep(0)
                assert not routing.is_set() and not trader.ready
            else:
                tasks.append(asyncio.create_task(trader.process_next()))
                await asyncio.wait_for(routing.wait(), 10)
                tasks.append(asyncio.create_task(maintenance()))
                await asyncio.wait_for(called.wait(), 10)
                assert not entered.is_set() and trader.ready
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            async with db.unit_of_work() as u:
                assert len(u.decisions.all()) == 4
                assert all(d.kind.value == "ADMITTED" for d in u.decisions.all())
            assert trader.can_enter() and trader.last_boundary == T
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await trader.close()

    asyncio.run(run())
