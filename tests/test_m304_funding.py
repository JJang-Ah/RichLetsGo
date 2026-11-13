"""Funding settlement positions survive late delivery and current-book changes."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_oms import execution, order
from test_risk import NOW

from richletsgo.domain import Environment, StrategyId, Symbol
from richletsgo.portfolio.funding import FundingSnapshotMissing, allocate, position_snapshot
from richletsgo.portfolio.models import StrategyBook
from richletsgo.storage.contracts import FillRecord, FillStatus
from richletsgo.storage.memory import InMemoryDatabase
from richletsgo.storage.sqlite import SqliteDatabase


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("flat", [False, True])
def test_late_funding_allocates_settlement_holder_once(tmp_path, backend, flat):
    async def run():
        db = (
            SqliteDatabase(tmp_path / "db.sqlite", "a", Environment.DEMO, now=lambda: NOW)
            if backend == "sqlite"
            else InMemoryDatabase("a", Environment.DEMO)
        )
        if backend == "sqlite":
            await db.migrate()
        async with db.unit_of_work() as u:
            u.portfolio.put_book(StrategyBook(StrategyId.S1, Symbol.BTCUSDT, D(2), D(100)))
            position_snapshot(u, NOW, Symbol.BTCUSDT)
            await u.commit()
        async with db.unit_of_work() as u:
            u.portfolio.put_book(StrategyBook(StrategyId.S1, Symbol.BTCUSDT))
            if not flat:
                u.portfolio.put_book(StrategyBook(StrategyId.S2, Symbol.BTCUSDT, D(2), D(110)))
            allocate(u, "f", Symbol.BTCUSDT, D("0.3"), NOW)
            allocate(u, "f", Symbol.BTCUSDT, D("0.3"), NOW)
            await u.commit()
        async with db.unit_of_work() as u:
            assert u.portfolio.book(StrategyId.S1, Symbol.BTCUSDT).funding == D("0.3")
            assert u.portfolio.book(StrategyId.S2, Symbol.BTCUSDT).funding == 0

    asyncio.run(run())


def test_snapshot_reverses_later_fill_and_waits_for_pending():
    async def run():
        db = InMemoryDatabase("a", Environment.DEMO)
        event = replace(execution(order()), exec_time=NOW + timedelta(seconds=1))
        async with db.unit_of_work() as u:
            u.portfolio.put_book(StrategyBook(StrategyId.S1, Symbol.BTCUSDT, D(3), D(100)))
            u.fills.add(FillRecord(event, FillStatus.BOOKED, None, StrategyId.S1))
            u.recovery.put("position_delta:" + event.exec_id, json.dumps({"S1": "1", "S2": "0"}))
            assert position_snapshot(u, NOW, Symbol.BTCUSDT) == {"S1": "2", "S2": "0"}
            await u.commit()
        pending = replace(event, exec_id="pending", exec_time=NOW - timedelta(seconds=2))
        async with db.unit_of_work() as u:
            u.fills.add(FillRecord(pending, FillStatus.PENDING_ATTRIBUTION, None, None))
            with pytest.raises(FundingSnapshotMissing):
                position_snapshot(u, NOW + timedelta(seconds=1), Symbol.BTCUSDT)

    asyncio.run(run())


def test_zero_snapshot_records_unallocated_and_midnight_day():
    async def run():
        db = InMemoryDatabase("a", Environment.DEMO)
        midnight = NOW.replace(hour=0)
        async with db.unit_of_work() as u:
            with pytest.raises(FundingSnapshotMissing):
                allocate(u, "missing", Symbol.BTCUSDT, D(1), midnight)
            for index, at in enumerate(
                (midnight - timedelta(seconds=1), midnight, midnight + timedelta(seconds=1))
            ):
                position_snapshot(u, at, Symbol.BTCUSDT)
                allocate(u, str(index), Symbol.BTCUSDT, D(1), at)
            await u.commit()
        async with db.unit_of_work() as u:
            records = [
                json.loads(u.recovery.get("unallocated_funding:" + str(i))) for i in range(3)
            ]
            assert records[0]["day"] == records[1]["day"]
            assert records[1]["day"] != records[2]["day"]
            assert len(u.portfolio.actions()) == 3

    asyncio.run(run())
