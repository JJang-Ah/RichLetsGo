"""Control cancellation uses the actual M1 outbox, journal and simulated exchange."""

import asyncio
from datetime import timedelta

import pytest
import test_portfolio
from test_recovery_integration import admit, setup, start
from test_risk import NOW

from richletsgo.control.models import CommandPolicy, Context
from richletsgo.control.service import CommandService
from richletsgo.domain import Environment, OrderState
from richletsgo.storage.sqlite import SqliteDatabase


@pytest.mark.parametrize("after_dispatch", [False, True])
@pytest.mark.parametrize("source", ["telegram", "web"])
def test_cancel_crash_recovers_fixed_targets_using_real_dispatch(
    tmp_path, monkeypatch, after_dispatch, source
):
    async def run():
        db = SqliteDatabase(tmp_path / "control.sqlite", "acct", Environment.DEMO, now=lambda: NOW)
        await db.migrate()
        monkeypatch.setattr(test_portfolio, "InMemoryDatabase", lambda *args: db)
        _, clock, _, router, _, sync = setup(tmp_path / "journal")
        notices = []

        async def notify(raw):
            notices.append(raw)

        def commands():
            return CommandService(
                account_id="acct",
                environment=Environment.DEMO,
                uow_factory=db.unit_of_work,
                pauses=router.gate.pauses,
                tracker=router.gate.tracker,
                limits=router.gate.limits,
                allowed_ids={"web:owner", "telegram:1"},
                clock=clock,
                policy=CommandPolicy(timedelta(seconds=5), 2),
                account_lock=router.gate.account_lock,
                notify=notify,
                resolve_commit=db.resolve,
            )

        try:
            await start(sync)
            entry = await admit(router)
            await sync.sender.send(entry)
            await sync.tick()
            service = commands()
            ctx = Context(
                source,
                "web:owner" if source == "web" else "telegram:1",
                "session",
                Environment.DEMO,
                "acct",
                "cancel-once",
            )
            prompt = await service.request(ctx, "cancel")
            accepted = await service.request(
                ctx, "cancel", confirmation_id=prompt["confirmation_id"]
            )
            assert accepted["state"] == "accepted"
            if after_dispatch:
                await sync._actions(NOW)
                # Stop before CommandService has interpreted acknowledgement/terminal events.
            restarted = commands()
            await restarted.startup()
            await sync.synchronize()
            await restarted.tick()
            result = await restarted.request(ctx, "cancel")
            assert result["state"] == "completed"
            assert result["targets"][0]["final_status"] == OrderState.CANCELLED.value
            assert result["targets"][0]["attempts"] == 1
            async with db.unit_of_work() as uow:
                assert uow.orders.get(entry.key).state is OrderState.CANCELLED
                assert not [a for a in uow.portfolio.actions() if a.key.startswith("cmd:")]
        finally:
            await sync.close()

    asyncio.run(run())
