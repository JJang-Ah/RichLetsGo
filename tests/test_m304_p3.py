"""Deferred storage/control contracts, entirely synthetic."""

import asyncio
import io
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from importlib.resources import files
from urllib.error import HTTPError

import pytest
from test_control import setup
from test_oms import order
from test_risk import NOW

from richletsgo.control.service import CommandService
from richletsgo.domain import Environment, OrderState, StrategyId, Symbol
from richletsgo.keystore import Secret
from richletsgo.portfolio.models import ProtectionRecovery
from richletsgo.storage.errors import StorageError
from richletsgo.storage.sqlite import SqliteDatabase, SqliteUnitOfWork, open_readonly, statements
from richletsgo_dashboard import control


def test_sql_split_handles_literals_triggers_and_incomplete_sql():
    script = "CREATE TABLE a(v); CREATE TABLE b(v); CREATE TRIGGER mirror AFTER INSERT ON a BEGIN INSERT INTO b VALUES ('x;y'); INSERT INTO b VALUES(new.v); END; INSERT INTO a VALUES('z;z');"
    with sqlite3.connect(":memory:") as c:
        for statement in statements(script):
            c.execute(statement)
        assert c.execute("SELECT v FROM b").fetchall() == [("x;y",), ("z;z",)]
    with pytest.raises(StorageError):
        statements("CREATE TABLE incomplete(")


def test_v1_identity_migration_preserves_legacy_rows(tmp_path):
    path = tmp_path / "legacy.sqlite"
    c = sqlite3.connect(path)
    script = files("richletsgo.storage").joinpath("migrations/V001__init.sql").read_text()
    c.executescript(script)
    c.execute("INSERT INTO schema_version VALUES(1,0)")
    c.executemany("INSERT INTO meta VALUES(?,?)", [("environment", "demo"), ("account_id", "acct")])
    c.execute("INSERT INTO commands VALUES ('acct','legacy','telegram','completed','{}',0)")
    before = c.execute("SELECT * FROM commands").fetchall()
    c.commit()
    c.close()
    db = SqliteDatabase(path, "acct", Environment.DEMO)
    asyncio.run(db.migrate())
    asyncio.run(db.migrate())
    c = open_readonly(path)
    try:
        assert c.execute("SELECT * FROM commands").fetchall() == before
        assert c.execute("SELECT * FROM command_identities").fetchall() == [
            ("acct", "telegram", "legacy", "legacy")
        ]
        assert c.execute("SELECT version FROM schema_version").fetchall() == [(2,)]
    finally:
        c.close()


def test_successful_commit_cleanup_error_is_not_retryable(tmp_path, monkeypatch):
    async def run():
        alerts = []
        db = SqliteDatabase(tmp_path / "db.sqlite", "acct", Environment.DEMO, alert=alerts.append)
        await db.migrate()
        original = SqliteUnitOfWork._close_worker
        fired = False

        def cleanup(uow):
            nonlocal fired
            had_connection = uow._connection is not None
            original(uow)
            if had_connection and not fired:
                fired = True
                raise StorageError("synthetic cleanup failure")

        monkeypatch.setattr(SqliteUnitOfWork, "_close_worker", cleanup)
        async with db.unit_of_work() as uow:
            uow.recovery.put("marker", "committed")
            await uow.commit()
        assert alerts and not db.entry_paused
        async with db.unit_of_work() as uow:
            assert uow.recovery.get("marker") == "committed"

    asyncio.run(run())


def test_recovery_retains_old_unreferenced_order(tmp_path):
    async def run():
        db = SqliteDatabase(tmp_path / "db.sqlite", "acct", Environment.DEMO, now=lambda: NOW)
        await db.migrate()
        old = NOW - timedelta(days=30)
        record = replace(order(state=OrderState.FILLED), created_at=old, updated_at=old)
        async with db.unit_of_work() as uow:
            uow.orders.put(record)
            uow.portfolio.put_recovery(
                StrategyId.S1, Symbol.BTCUSDT, ProtectionRecovery("r", 0, NOW)
            )
            await uow.commit()
        async with db.unit_of_work() as uow:
            assert uow.orders.get(record.key) == record

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_equal_external_ids_are_independent_by_source(tmp_path, backend):
    async def run():
        db, svc, ctx, _, notices = await setup(tmp_path, backend)
        web = await svc.request(ctx, "pause")
        tg_ctx = replace(ctx, source="telegram", actor_id="telegram:1")
        tg = await svc.request(tg_ctx, "pause")
        assert web["request_id"] == tg["request_id"]
        assert web["storage_key"] != tg["storage_key"]
        assert await svc.request(ctx, "pause") == web
        assert await svc.request(tg_ctx, "pause") == tg
        assert len(notices) == 2

    asyncio.run(run())


def test_pending_resolution_does_not_hold_account_lock(tmp_path):
    async def run():
        db, svc, ctx, _, _ = await setup(tmp_path)

        def fault(stage, c):
            if stage == "after_commit":
                raise OSError("uncertain")

        db.fault = fault
        assert (await svc.request(ctx, "pause"))["state"] == "confirming"
        db.fault = None
        entered, release = asyncio.Event(), asyncio.Event()
        original = svc.resolve_commit

        async def delayed(txn):
            entered.set()
            await release.wait()
            return await original(txn)

        svc.resolve_commit = delayed
        pending = asyncio.create_task(svc.request(ctx, "pause"))
        await entered.wait()
        async with asyncio.timeout(1):
            async with svc.lock:
                pass
        release.set()
        assert (await pending)["state"] == "completed"

    asyncio.run(run())


def test_health_cache_expires_and_detail_4xx_is_rejected(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(control.time, "monotonic", lambda: now[0])
    client = control.HttpTraderClient(
        port=8766,
        dashboard_port=8765,
        token=Secret("synthetic"),
        environment="demo",
        account_id="acct",
    )
    calls = []

    def call(path, body=None):
        calls.append(path)
        return {"environment": "demo", "account_id": "acct"}

    monkeypatch.setattr(client, "_call", call)
    assert client.connected() and client.connected() and len(calls) == 1
    now[0] += 2
    assert client.connected() and len(calls) == 2
    monkeypatch.undo()

    def invalid(request, **kwargs):
        raise HTTPError(
            request.full_url,
            422,
            "invalid",
            {},
            io.BytesIO(json.dumps({"detail": "validation"}).encode()),
        )

    monkeypatch.setattr(control, "urlopen", invalid)
    result = control.HttpTraderClient._call(client, "/commands/pause", {})
    assert result["state"] == "rejected" and "요청 형식 거절" in result["message"]


def test_cancel_all_filled_summary():
    record = {"targets": [{"state": "confirmed", "final_status": "FILLED"}]}
    CommandService._finish(record)
    assert record["message"] == "취소 전에 전부 체결됨"
