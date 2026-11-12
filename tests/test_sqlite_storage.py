"""Synthetic persistence and executor fault tests; never touches an operational database."""

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from richletsgo.domain import Environment, Qty, StrategyId, Symbol
from richletsgo.storage.codec import decode, encode
from richletsgo.storage.errors import CommitOutcomeUnknown
from richletsgo.storage.sqlite import SqliteDatabase, open_readonly

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def database(tmp_path, **kwargs):
    return SqliteDatabase(
        tmp_path / "trading.sqlite", "test", Environment.DEMO, now=lambda: NOW, **kwargs
    )


def test_commit_rollback_restart_readonly_backup(tmp_path):
    async def run():
        db = database(tmp_path)
        await db.migrate()
        await db.migrate()
        async with db.unit_of_work() as uow:
            uow.positions.set(StrategyId.S1, Symbol.BTCUSDT, Qty(Decimal("0.10000000")))
            await uow.commit()
        async with db.unit_of_work() as uow:
            uow.positions.set(StrategyId.S1, Symbol.BTCUSDT, Qty(Decimal("1e-20")))
        db = database(tmp_path)
        async with db.unit_of_work() as uow:
            assert uow.positions.get(StrategyId.S1, Symbol.BTCUSDT).value == Decimal("0.1")
            connection = open_readonly(db.path)
            try:
                assert connection.execute("SELECT count(*) FROM positions").fetchone() == (1,)
                with pytest.raises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM positions")
            finally:
                connection.close()
        backup = await db.backup(tmp_path / "backup")
        connection = open_readonly(backup)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        finally:
            connection.close()

    asyncio.run(run())


@pytest.mark.parametrize("committed", [False, True])
def test_unknown_commit_is_not_retried_and_resolves(tmp_path, committed):
    def fault(stage, connection):
        if stage == ("after_commit" if committed else "commit"):
            raise OSError("synthetic lost outcome")

    async def run():
        db = database(tmp_path, fault=fault)
        await db.migrate()
        with pytest.raises(CommitOutcomeUnknown) as error:
            async with db.unit_of_work() as uow:
                uow.recovery.put("test:key", "value")
                await uow.commit()
        assert db.entry_paused
        assert await db.resolve(error.value.txn_id) is committed
        assert db.entry_paused  # result alone cannot resume normal dispatch before journal merge
        async with db.unit_of_work() as uow:
            assert uow.recovery.get("test:key") == ("value" if committed else None)
        db.reconciled(error.value.txn_id)
        assert not db.entry_paused

    asyncio.run(run())


def test_precommit_failure_is_rollback(tmp_path):
    def fault(stage, connection):
        if stage == "before_commit":
            raise OSError("synthetic precommit")

    async def run():
        db = database(tmp_path, fault=fault)
        await db.migrate()
        with pytest.raises(OSError):
            async with db.unit_of_work() as uow:
                uow.recovery.put("test:key", "value")
                await uow.commit()
        assert not db.entry_paused
        db.fault = None
        async with db.unit_of_work() as uow:
            assert uow.recovery.get("test:key") is None
            uow.recovery.put("test:key", "value")
            await uow.commit()

    asyncio.run(run())


def test_two_transactions_do_not_deadlock_worker(tmp_path):
    threads = set()

    def fault(stage, connection):
        threads.add(threading.get_ident())

    async def run():
        db = database(tmp_path, fault=fault, busy_timeout_seconds=0.01)
        await db.migrate()
        first_loaded = asyncio.Event()
        second_started = asyncio.Event()

        async def first():
            async with db.unit_of_work() as uow:
                first_loaded.set()
                await second_started.wait()
                await asyncio.sleep(0.03)
                uow.recovery.put("counter:n", "1")
                await uow.commit()

        async def second():
            await first_loaded.wait()
            second_started.set()
            async with db.unit_of_work() as uow:
                assert uow.recovery.get("counter:n") == "1"
                await uow.commit()

        await asyncio.wait_for(asyncio.gather(first(), second()), 5)
        assert len(threads) == 1 and threading.get_ident() not in threads

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["before_begin", "before_load", "before_flush", "commit"])
def test_cancel_worker_is_drained_before_reuse(tmp_path, stage):
    entered, release = threading.Event(), threading.Event()

    def fault(point, connection):
        if point == stage:
            entered.set()
            assert release.wait(5)

    async def run():
        db = database(tmp_path, fault=fault)
        await db.migrate()

        async def transaction():
            async with db.unit_of_work() as uow:
                uow.recovery.put("test:key", "value")
                await uow.commit()

        task = asyncio.create_task(transaction())
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        assert db._lock.locked()
        release.set()
        with pytest.raises(CommitOutcomeUnknown if stage == "commit" else asyncio.CancelledError):
            await task
        db.fault = None
        async with db.unit_of_work() as uow:
            assert uow.recovery.get("test:key") == ("value" if stage == "commit" else None)

    asyncio.run(run())


def test_codec_exact_decimal_and_utc_milliseconds():
    for value in (Decimal("0.10000000"), Decimal("1e-28"), NOW):
        assert decode(encode(value)) == value
    assert "1E-" not in encode(Decimal("1e-28"))


@pytest.mark.parametrize("folder", ["OneDrive", "Dropbox", "Google Drive", "iCloud"])
def test_sync_directory_rejected(tmp_path, folder):
    with pytest.raises(ValueError):
        database(tmp_path / folder)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_working_set_references_and_archived_duplicate(tmp_path, backend):
    from dataclasses import replace
    from datetime import timedelta

    from test_oms import execution, order

    from richletsgo.domain import OrderState
    from richletsgo.risk.records import Direction, EpisodeRecord, EpisodeState
    from richletsgo.storage.contracts import FillRecord, FillStatus
    from richletsgo.storage.errors import DuplicateExecId
    from richletsgo.storage.memory import InMemoryDatabase

    async def run():
        db = (
            database(tmp_path)
            if backend == "sqlite"
            else InMemoryDatabase("test", Environment.DEMO, now=lambda: NOW)
        )
        if backend == "sqlite":
            await db.migrate()
        old = NOW - timedelta(days=30)
        archived = replace(order(state=OrderState.FILLED), created_at=old, updated_at=old)
        referenced = replace(archived, order_link_id="referenced", exchange_order_id="reference")
        fill = FillRecord(
            replace(execution(archived), exec_time=old),
            FillStatus.BOOKED,
            archived.key,
            StrategyId.S1,
        )
        async with db.unit_of_work() as uow:
            uow.orders.put(archived)
            uow.orders.put(referenced)
            uow.fills.add(fill)
            uow.episodes.put(
                EpisodeRecord(
                    "active",
                    StrategyId.S1,
                    Symbol.BTCUSDT,
                    Direction.LONG,
                    Qty(Decimal(1)),
                    old,
                    (referenced.key,),
                    EpisodeState.OPEN,
                )
            )
            await uow.commit()
        async with db.unit_of_work() as uow:
            assert uow.orders.get(archived.key) is None
            assert uow.orders.get(referenced.key) == referenced
            assert not uow.fills.all()
            uow.fills.add(fill)
            with pytest.raises(DuplicateExecId):
                await uow.commit()

    asyncio.run(run())


def test_mismatch_and_lock_timeout(tmp_path):
    from richletsgo.storage.errors import StorageBusy, StorageError

    async def run():
        db = database(tmp_path, busy_timeout_seconds=0.01)
        await db.migrate()
        other = SqliteDatabase(db.path, "test", Environment.LIVE)
        with pytest.raises(StorageError, match="environment"):
            await other.migrate()
        connection = sqlite3.connect(db.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(StorageBusy):
                async with db.unit_of_work():
                    pass
            connection.rollback()
            connection.execute("UPDATE schema_version SET version=9")
            connection.commit()
            with pytest.raises(StorageError, match="version"):
                async with db.unit_of_work():
                    pass
        finally:
            connection.close()

    asyncio.run(run())


def test_pending_marker_retention_resolution_failure_and_waiter_cancel(tmp_path):
    from datetime import timedelta

    async def run():
        alerts = []
        db = database(tmp_path, alert=alerts.append, outcome_timeout_seconds=0.01)
        await db.migrate()
        async with db.unit_of_work() as uow:
            uow.recovery.put("key", "value")
            txn = uow.txn_id
            await uow.commit()
        db._unknown(txn)
        db.now = lambda: NOW + timedelta(days=40)
        async with db.unit_of_work() as uow:
            # Waiting for the process lock is cancellable without acquiring a connection.
            task = asyncio.create_task(db.unit_of_work().__aenter__())
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(TimeoutError):
                await db.resolve(txn)
            assert db.entry_paused and alerts
            await uow.commit()
        # The 10ms deadline above intentionally tests a held lock, not disk speed.
        db.outcome_timeout_seconds = 5
        assert await db.resolve(txn) is True  # 40 days old, but still pending
        db.reconciled(txn)
        async with db.unit_of_work() as uow:
            await uow.commit()
        connection = open_readonly(db.path)
        try:
            assert (
                connection.execute("SELECT 1 FROM uow_commits WHERE txn_id=?", (txn,)).fetchone()
                is None
            )
        finally:
            connection.close()

    asyncio.run(run())


def test_old_signal_is_rejected_before_id_allocation():
    from datetime import timedelta

    from test_risk import figures, limits, market

    from richletsgo.domain import FixedClock, Price
    from richletsgo.risk.equity import AccountSnapshot, EquityTracker
    from richletsgo.risk.gate import EntryRequest, RiskGate
    from richletsgo.risk.pause import PauseState
    from richletsgo.risk.records import Direction, signal_key
    from richletsgo.storage.memory import InMemoryDatabase

    async def run():
        db = InMemoryDatabase("test", Environment.DEMO)
        gate = RiskGate(
            account_id="test",
            environment=Environment.DEMO,
            uow_factory=db.unit_of_work,
            limits=limits(),
            tracker=EquityTracker.open(AccountSnapshot(NOW, Decimal(1000), Decimal(0))),
            pauses=PauseState(),
            clock=FixedClock(NOW),
            unique_factory=lambda: pytest.fail("new ID"),
        )
        request = EntryRequest(
            signal_key(StrategyId.S1, Symbol.BTCUSDT, NOW - timedelta(days=8), "ENTRY"),
            StrategyId.S1,
            Symbol.BTCUSDT,
            Direction.LONG,
            Price(Decimal(100)),
            Price(Decimal(95)),
            Decimal(1),
        )
        with pytest.raises(ValueError, match="predates"):
            await gate.admit(request, market(), {StrategyId.S1: figures()})

    asyncio.run(run())


def test_cancelled_commit_can_finish_rolled_back(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def fault(stage, connection):
        if stage == "commit":
            entered.set()
            assert release.wait(5)
            connection.rollback()
            raise OSError("late rollback")

    async def run():
        db = database(tmp_path, fault=fault)
        await db.migrate()

        async def transaction():
            async with db.unit_of_work() as uow:
                uow.recovery.put("key", "value")
                await uow.commit()

        task = asyncio.create_task(transaction())
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        release.set()
        with pytest.raises(CommitOutcomeUnknown) as error:
            await task
        assert await db.resolve(error.value.txn_id) is False

    asyncio.run(run())


def test_memory_phase_cancellation_and_backup_retention(tmp_path):
    async def run():
        db = database(tmp_path)
        await db.migrate()
        with pytest.raises(asyncio.CancelledError):
            async with db.unit_of_work() as uow:
                uow.recovery.put("key", "discarded")
                raise asyncio.CancelledError
        async with db.unit_of_work() as uow:
            assert uow.recovery.get("key") is None
        directory = tmp_path / "backups"
        directory.mkdir()
        old = directory / "demo-2026-10-05.sqlite"
        retained = directory / "demo-2026-10-06.sqlite"
        old.write_bytes(b"synthetic old backup")
        retained.write_bytes(b"synthetic retained backup")
        await db.backup(directory)
        assert not old.exists() and retained.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "sql,exception",
    [
        ("INSERT INTO meta VALUES ('environment', 'duplicate')", sqlite3.IntegrityError),
        ("INSERT INTO missing_table VALUES (1)", sqlite3.OperationalError),
    ],
)
def test_flush_sqlite_failure_rolls_back_and_releases_lock(tmp_path, sql, exception):
    from richletsgo.storage.errors import StorageError

    async def run():
        db = database(tmp_path)
        await db.migrate()

        def fault(stage, connection):
            if stage == "before_commit":
                connection.execute(sql)

        db.fault = fault
        with pytest.raises(StorageError) as error:
            async with db.unit_of_work() as uow:
                uow.recovery.put("key", "discarded")
                await uow.commit()
        assert isinstance(error.value.__cause__, exception)
        assert not db.entry_paused
        db.fault = None
        async with db.unit_of_work() as uow:
            assert uow.recovery.get("key") is None
            uow.recovery.put("key", "recovered")
            await uow.commit()

    asyncio.run(run())


def test_rollback_sqlite_failure_releases_process_lock(tmp_path):
    from richletsgo.storage.errors import StorageError

    async def run():
        db = database(tmp_path)
        await db.migrate()
        with pytest.raises(StorageError) as error:
            async with db.unit_of_work() as uow:
                # A genuinely closed sqlite connection raises ProgrammingError on rollback.
                await db._submit(lambda: uow._connection.close())
        assert isinstance(error.value.__cause__, sqlite3.ProgrammingError)
        async with db.unit_of_work() as uow:
            await uow.commit()

    asyncio.run(run())
