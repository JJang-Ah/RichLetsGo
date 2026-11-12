"""Dashboard SQLite reader over a synthetic, temporary database (never a real one)."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from richletsgo.domain import Environment, OrderPurpose, OrderState, Price, Qty, StrategyId, Symbol
from richletsgo.domain.events import ExecutionEvent
from richletsgo.oms.models import OrderRecord
from richletsgo.portfolio.models import StopLink, StrategyBook
from richletsgo.risk.pause import Pause, PauseKind, PauseState
from richletsgo.risk.records import (
    DecisionKind,
    DecisionRecord,
    Direction,
    Rejection,
    RejectionCode,
)
from richletsgo.storage.contracts import FillRecord, FillStatus
from richletsgo.storage.sqlite import SqliteDatabase
from richletsgo_dashboard import sqlite_source
from richletsgo_dashboard.app import create_app
from richletsgo_dashboard.control import Disconnected
from richletsgo_dashboard.security import Auth
from richletsgo_dashboard.source import SourceUnavailable
from richletsgo_dashboard.sqlite_source import SqliteSource

NOW = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
PORT = 8765


def order(link: str, purpose: OrderPurpose, qty: str, state: OrderState, **extra) -> OrderRecord:
    return OrderRecord(
        link, f"x-{link}", None, StrategyId.S1, purpose, Symbol.BTCUSDT, Qty(D(qty)),
        extra.get("limit"), Price(D("96000")), state, Qty(D(0)), D(extra.get("cum", "0")),
        NOW - timedelta(minutes=30), NOW, trigger_price=extra.get("trigger"),
    )  # fmt: skip


async def seed(path) -> None:
    db = SqliteDatabase(path, "demo-sub-1", Environment.DEMO, now=lambda: NOW)
    await db.migrate()
    entry = order("D-S1-E-1", OrderPurpose.ENTRY, "0.050", OrderState.FILLED, cum="0.050")
    stop = order(
        "D-S1-SL-1", OrderPurpose.ATTACHED_SL, "-0.050", OrderState.OPEN, trigger=Price(D("94000"))
    )
    lost = order("D-S1-E-2", OrderPurpose.ENTRY, "0.020", OrderState.UNKNOWN)
    async with db.unit_of_work() as uow:
        for record in (entry, stop, lost):
            uow.orders.put(record)
        uow.fills.add(
            FillRecord(
                ExecutionEvent(
                    "exec-1", entry.order_link_id, "x-D-S1-E-1", Symbol.BTCUSDT,
                    Qty(D("0.050")), Price(D("96120.5")), D("2.643814"), NOW - timedelta(minutes=29),
                    False,
                ),
                FillStatus.BOOKED, entry.key, StrategyId.S1,
            )
        )  # fmt: skip
        uow.portfolio.put_book(
            StrategyBook(
                StrategyId.S1, Symbol.BTCUSDT, D("0.050"), D("96120.5"),
                stop_price=Price(D("94000")), stops=(StopLink(stop.key, D("0.030")),),
            )
        )  # fmt: skip
        uow.decisions.add(
            DecisionRecord(
                "demo-sub-1", "S1|BTCUSDT|2026-11-12T02:00:00+00:00|entry", StrategyId.S1,
                Symbol.BTCUSDT, Direction.LONG, DecisionKind.REJECTED, NOW, None, None,
                (Rejection(RejectionCode.PAUSED, "storage"),), (),
            )
        )  # fmt: skip
        pauses = PauseState((Pause(PauseKind.ENTRY_PAUSED_SYSTEM, "unknown_order:BTCUSDT", NOW),))
        uow.recovery.put("pauses", json.dumps(pauses.to_json()))
        await uow.commit()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "demo" / "trading.sqlite"
    asyncio.run(seed(path))
    return path


def client(source) -> TestClient:
    app = create_app(source, Auth(code=lambda: None, clock=lambda: 0.0), Disconnected(), port=PORT)
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}")


def test_snapshot_maps_stored_rows_without_inventing_figures(db_path):
    snap = SqliteSource(db_path, "demo").snapshot()
    o = snap.overview
    assert (o.account_id, o.pause_state, o.sample) == ("demo-sub-1", "ENTRY_PAUSED_SYSTEM", False)
    assert o.pause_reasons == ("unknown_order:BTCUSDT",)
    assert o.equity is None and o.today_pnl is None and o.limits == ()  # not persisted yet
    assert o.last_fill == NOW - timedelta(minutes=29)
    (position,) = snap.positions
    assert (position.side, position.quantity, position.entry_price) == (
        "Buy",
        D("0.050"),
        D("96120.5"),
    )
    assert position.exchange_stop_quantity == D("0.030") and not position.fully_protected
    statuses = {r.order_link_id: (r.purpose, r.side, r.status, r.price) for r in snap.orders}
    assert statuses["D-S1-SL-1"] == ("손절", "Sell", "OPEN", D("94000"))
    assert statuses["D-S1-E-2"][2] == "UNKNOWN"
    assert snap.fills[0].fee == D("2.643814")  # Decimal straight from the stored text
    assert snap.decisions[0].reason == "REJECTED: PAUSED"
    assert snap.logs[0].kind == "pause"


def test_screens_render_from_sqlite_without_sample_banner(db_path):
    c = client(SqliteSource(db_path, "demo"))
    home = c.get("/").text
    assert "샘플 데이터입니다" not in home and "demo-sub-1" in home
    assert "UNKNOWN 주문 1건" in home and "거래소 손절이 보유 수량과 다른 포지션 1개" in home
    assert "Trader가 한도 사용률을 저장하면" in home
    for path in ("/positions", "/equity", "/orders", "/logs", "/decisions", "/control"):
        assert c.get(path).status_code == 200, path
    assert "96,120.5" in c.get("/positions").text
    assert c.get("/api/overview").json()["equity"] == "-"


def test_reader_never_writes_and_rejects_wrong_environment(db_path, tmp_path):
    before = db_path.read_bytes()
    SqliteSource(db_path, "demo").snapshot()
    assert db_path.read_bytes() == before
    with pytest.raises(SourceUnavailable):
        SqliteSource(db_path, "live").snapshot()
    with pytest.raises(SourceUnavailable):
        SqliteSource(tmp_path / "missing.sqlite", "demo").snapshot()
    r = client(SqliteSource(db_path, "live")).get("/")
    assert r.status_code == 503 and "environment" in r.text


def test_reader_sees_commits_while_a_writer_holds_the_file(db_path):
    writer = sqlite3.connect(db_path)
    writer.execute("BEGIN IMMEDIATE")  # the Trader mid-transaction must not block reads
    try:
        assert SqliteSource(db_path, "demo").snapshot().overview.account_id == "demo-sub-1"
    finally:
        writer.rollback()
        writer.close()


def test_snapshot_reads_one_consistent_moment(db_path, monkeypatch):
    async def close_position() -> None:
        # One Trader transaction touching two tables: a new order and the book going flat.
        db = SqliteDatabase(db_path, "demo-sub-1", Environment.DEMO, now=lambda: NOW)
        async with db.unit_of_work() as uow:
            uow.orders.put(order("D-S1-X-1", OrderPurpose.CLOSE, "-0.050", OrderState.FILLED))
            uow.portfolio.put_book(StrategyBook(StrategyId.S1, Symbol.BTCUSDT))
            await uow.commit()

    real, fired = sqlite_source.decode, []

    def decode_then_commit(payload):
        if not fired:  # the Trader commits while the dashboard is midway through its reads
            fired.append(True)
            asyncio.run(close_position())
        return real(payload)

    monkeypatch.setattr(sqlite_source, "decode", decode_then_commit)
    snap = SqliteSource(db_path, "demo").snapshot()
    assert fired
    assert (len(snap.orders), len(snap.positions)) == (3, 1)  # all before, never half of each
    monkeypatch.setattr(sqlite_source, "decode", real)
    after = SqliteSource(db_path, "demo").snapshot()
    assert (len(after.orders), len(after.positions)) == (4, 0)


def test_unresolved_orders_survive_the_row_limit(db_path):
    async def settle_many() -> None:
        db = SqliteDatabase(db_path, "demo-sub-1", Environment.DEMO, now=lambda: NOW)
        async with db.unit_of_work() as uow:
            for i in range(sqlite_source.ROWS + 1):
                uow.orders.put(
                    order(f"D-S1-E-c{i}", OrderPurpose.ENTRY, "0.001", OrderState.CANCELLED)
                )
            await uow.commit()

    asyncio.run(settle_many())
    snap = SqliteSource(db_path, "demo").snapshot()
    states = [r.status for r in snap.orders]
    assert states.count("UNKNOWN") == 1 and states.count("OPEN") == 1
    assert states.count("CANCELLED") == sqlite_source.ROWS
    c = client(SqliteSource(db_path, "demo"))
    assert "UNKNOWN 주문 1건" in c.get("/").text
    assert "D-S1-E-2" in c.get("/orders?status=unknown").text
    assert "D-S1-SL-1" in c.get("/orders?status=open").text
