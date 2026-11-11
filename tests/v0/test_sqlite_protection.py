"""Late commit resolution must not replay journal-owned create/amend/cancel operations."""

import asyncio
from dataclasses import replace
from decimal import Decimal as D

import pytest
import test_portfolio
from test_recovery_integration import filled_account
from test_risk import BTC, NOW

from richletsgo.domain import Environment, OrderPurpose, OrderState, Price, Qty
from richletsgo.portfolio.models import Action, ActionKind
from richletsgo.storage.sqlite import SqliteDatabase, open_readonly


@pytest.mark.parametrize("committed", [False, True])
def test_entry_unknown_waits_for_marker_without_new_id(tmp_path, monkeypatch, committed):
    from test_recovery_integration import admit, setup, start

    from richletsgo.exchange.contracts import Accepted, ErrorCode

    async def run():
        db = SqliteDatabase(tmp_path / "entry.sqlite", "acct", Environment.DEMO, now=lambda: NOW)
        await db.migrate()
        monkeypatch.setattr(test_portfolio, "InMemoryDatabase", lambda *args: db)
        _, _, _, router, ex, sync = setup(tmp_path / "journal")
        try:
            await start(sync)
            order = await admit(router)
            fired = False

            def fault(stage, connection):
                nonlocal fired
                if not fired and stage == ("after_commit" if committed else "commit"):
                    fired = True
                    raise OSError("synthetic COMMIT failure")

            db.fault = fault
            result = await sync.sender.send(order)
            assert result.code is ErrorCode.TIMEOUT
            assert not await ex.get_open_orders(BTC)
            assert db.entry_paused
            txn = next(iter(sync.sender.pending_entries))
            db.fault = None
            resumed = await sync.sender.resume_entry_commit(txn, db.resolve)
            assert isinstance(resumed, Accepted) is committed
            orders = await ex.get_open_orders(BTC)
            assert len(orders) == int(committed)
            if committed:
                assert orders[0].order_link_id == order.order_link_id
            await sync.sender.resume_entry_commit(txn, db.resolve)
            assert len(await ex.get_open_orders(BTC)) == int(committed)
        finally:
            await sync.close()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["create", "amend", "cancel"])
@pytest.mark.parametrize("committed", [False, True])
def test_protection_journal_precedes_dispatch_after_unknown(
    tmp_path, monkeypatch, operation, committed
):
    async def run():
        db = SqliteDatabase(tmp_path / "test.sqlite", "acct", Environment.DEMO, now=lambda: NOW)
        await db.migrate()
        monkeypatch.setattr(test_portfolio, "InMemoryDatabase", lambda *args: db)
        _, clock, svc, router, ex, sync = await filled_account(tmp_path / "journal")
        try:
            async with db.unit_of_work() as uow:
                stop = next(o for o in uow.orders.all() if o.purpose is OrderPurpose.ATTACHED_SL)
                if operation == "create":
                    stop = replace(
                        stop,
                        order_link_id="d-S1-PROTECT-extra",
                        exchange_order_id=None,
                        purpose=OrderPurpose.PROTECT,
                        state=OrderState.CREATED,
                        qty=Qty(D(-1)),
                        booked_qty=Qty(D(0)),
                        exchange_cum_exec_qty=D(0),
                        trigger_by="LastPrice",
                    )
                    uow.orders.put(stop)
                key = "test:" + operation
                kind = {
                    "create": ActionKind.PROTECT,
                    "amend": ActionKind.AMEND_STOP,
                    "cancel": ActionKind.CANCEL_STOP,
                }[operation]
                uow.portfolio.emit(Action(key, kind, stop.strategy, BTC, order_key=stop.key))
                await uow.commit()
            fired = False

            def fault(stage, connection):
                nonlocal fired
                if not fired and stage == ("after_commit" if committed else "commit"):
                    fired = True
                    raise OSError("synthetic uncertain COMMIT")

            db.fault = fault
            if operation == "create":
                await sync.sender.send(stop, action_key=key)
            elif operation == "amend":
                await sync.sender.amend(stop, trigger=Price(D(91)), action_key=key)
            else:
                await sync.sender.cancel(stop, action_key=key)
            assert fired and db.entry_paused
            journal_before = sync.journal.read()
            assert len(journal_before) == 2
            txn = next(iter(db._file.unresolved))
            db.fault = None
            assert await db.resolve(txn) is committed
            records = await sync._load_journal()
            async with db.unit_of_work() as uow:
                assert key not in {a.key for a in uow.portfolio.actions()}
            await sync._refresh(BTC, NOW)
            await sync._drain()
            await sync._replay_outcomes(records)
            db.reconciled(txn)
            await sync._actions(NOW)
            async with db.unit_of_work() as uow:
                actual = uow.orders.get(stop.key)
                if operation == "amend":
                    assert actual.trigger_price == Price(D(91))
                elif operation == "cancel":
                    assert actual.state.is_terminal
                else:
                    assert actual.exchange_order_id is not None
            connection = open_readonly(db.path)
            try:
                assert (
                    connection.execute("SELECT count(*) FROM protective_operations").fetchone()[0]
                    >= 2
                )
            finally:
                connection.close()
            # Archival can remove journal lines, but cannot append a second operation.
            assert {r.operation_id for r in sync.journal.read()} <= {
                r.operation_id for r in journal_before
            }
        finally:
            await sync.close()

    asyncio.run(run())
