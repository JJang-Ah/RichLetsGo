"""V0 paths use the actual gate, portfolio, gateway, memory UoW and journal."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_portfolio import RISK_INPUT, signal
from test_portfolio import setup as portfolio_setup
from test_risk import BTC, NOW, counter, market

from richletsgo.domain import OrderPurpose, OrderState, Price, Qty, StrategyId
from richletsgo.exchange.contracts import Accepted, ErrorCode
from richletsgo.exchange.simulated import Fault, FaultPlan, SimulatedExchange
from richletsgo.exchange.simulated import FaultKind as F
from richletsgo.marketdata.models import Bar, Interval
from richletsgo.oms.journal import JournalCorrupt, JournalRecord, ProtectiveJournal
from richletsgo.oms.synchronizer import Synchronizer, SyncPolicy
from richletsgo.storage.memory import InMemoryUnitOfWork

POLICY = SyncPolicy(
    timedelta(days=7), timedelta(minutes=5), timedelta(seconds=20), timedelta(seconds=5), 3
)


def setup(tmp_path, faults=()):
    db, clock, svc, router = portfolio_setup()
    ex = SimulatedExchange(
        instruments=[market().instrument],
        clock=clock,
        wallet_balance=D(1000),
        maker_fee=D(".001"),
        taker_fee=D(".002"),
        slippage=lambda p, q: p,
        faults=FaultPlan(tuple(faults)),
    )
    sync = Synchronizer(
        db.unit_of_work,
        ex,
        svc,
        ProtectiveJournal(tmp_path),
        clock,
        router.gate.pauses,
        counter(),
        POLICY,
        (BTC,),
    )
    return db, clock, svc, router, ex, sync


def bar(clock, ex, *, low="95"):
    start = clock.now().replace(minute=0, second=0, microsecond=0)
    if start < clock.now():
        start += timedelta(hours=1)
    b = Bar(BTC, Interval.HOUR, start, D(100), D(105), D(low), D(100), D(1), D(100))
    clock.set(b.end)
    ex.advance_bar(b)


async def book(db):
    async with db.unit_of_work() as u:
        return u.portfolio.book(signal().strategy, BTC)


async def admit(router):
    result = await router.handle(signal(), market(), RISK_INPUT)
    assert result.order is not None
    return result.order


async def start(sync):
    assert await sync.synchronize()


def test_entry_fill_restart_adopts_attached_stop_and_deduplicates(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = setup(tmp_path)
        await start(sync)
        order = await admit(router)
        assert isinstance(await sync.sender.send(order), Accepted)
        bar(clock, ex)
        await sync.close()  # events not consumed into DB
        fresh = Synchronizer(
            db.unit_of_work,
            ex,
            svc,
            ProtectiveJournal(tmp_path),
            clock,
            router.gate.pauses,
            counter(),
            POLICY,
            (BTC,),
        )
        assert not await fresh.synchronize()  # booked fill makes first snapshot stale
        clock.advance(timedelta(seconds=1))
        assert await fresh.synchronize()
        before = await book(db)
        assert before.qty == 1 and sum(s.qty for s in before.stops) == 1
        clock.advance(timedelta(seconds=1))
        assert await fresh.synchronize()
        assert (await book(db)).qty == 1 and (await book(db)).fees == before.fees
        await fresh.close()

    asyncio.run(scenario())


def test_created_restart_aborts_without_signal_reentry(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = setup(tmp_path)
        await start(sync)
        order = await admit(router)
        await sync.close()
        fresh = Synchronizer(
            db.unit_of_work,
            ex,
            svc,
            ProtectiveJournal(tmp_path),
            clock,
            router.gate.pauses,
            counter(),
            POLICY,
            (BTC,),
        )
        clock.advance(timedelta(seconds=1))
        assert await fresh.synchronize()
        async with db.unit_of_work() as u:
            assert u.orders.get(order.key).state is OrderState.ABORTED
        again = await router.handle(signal(), market(), RISK_INPUT)
        assert again.decision.order_key == order.key
        assert (await ex.get_order_history(BTC, NOW)) == ()
        await fresh.close()

    asyncio.run(scenario())


def test_unknown_keeps_reservation_and_periodic_cancel_across_restart(tmp_path):
    async def scenario():
        faults = [
            Fault("place", 1, F.RESPONSE_LOST),
            Fault("place", 1, F.HIDE_ORDER, value=D(50)),
            Fault("cancel", 1, F.CANCEL_NOT_FOUND),
            Fault("cancel", 2, F.CANCEL_NOT_FOUND),
        ]
        db, clock, svc, router, ex, sync = setup(tmp_path, faults)
        await start(sync)
        ex.disconnect()
        order = await admit(router)
        assert (await sync.sender.send(order)).code is ErrorCode.TIMEOUT
        # No stream OPEN, hidden REST; empty queries never assert absence.
        await sync.tick()
        async with db.unit_of_work() as u:
            assert u.orders.get(order.key).state is OrderState.UNKNOWN
            assert u.orders.get(order.key).reserved_qty == 1
            raw = u.recovery.get("unknown:" + order.key)
        clock.advance(timedelta(seconds=1))
        await sync.tick()
        async with db.unit_of_work() as u:
            assert u.recovery.get("unknown:" + order.key) == raw
        clock.advance(timedelta(seconds=5))
        await sync.tick()
        async with db.unit_of_work() as u:
            assert (
                json.loads(u.recovery.get("unknown:" + order.key))["last"]
                != json.loads(raw)["last"]
            )
        await sync.close()

    asyncio.run(scenario())


def test_late_attached_connection_does_not_duplicate_protection(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = setup(tmp_path, [Fault("place", 1, F.HOLD_ATTACHED)])
        await start(sync)
        order = await admit(router)
        await sync.sender.send(order)
        bar(clock, ex)
        await sync.tick()
        clock.advance(timedelta(seconds=1))
        await sync.tick()
        ex.release_held()
        await sync.tick()
        all_orders = await ex.get_order_history(BTC, NOW)
        assert len(all_orders) == 2
        assert (await book(db)).qty == 1
        assert len((await book(db)).stops) == 1
        await sync.close()

    asyncio.run(scenario())


def test_commit_failure_entry_not_sent_protection_is_journaled(tmp_path, monkeypatch):
    async def scenario():
        db, clock, svc, router, ex, sync = setup(tmp_path)
        await start(sync)
        order = await admit(router)
        original = InMemoryUnitOfWork.commit

        async def fail(self):
            raise OSError("DB unavailable")

        monkeypatch.setattr(InMemoryUnitOfWork, "commit", fail)
        assert (await sync.sender.send(order)).code is ErrorCode.REJECTED
        assert await ex.get_open_orders(BTC) == ()
        assert sync.journal.read() == ()
        monkeypatch.setattr(InMemoryUnitOfWork, "commit", original)
        sync.pauses.clear(next(iter(sync.pauses.active())).kind, reason="db")
        await sync.sender.send(order)
        bar(clock, ex)
        await sync.tick()
        async with db.unit_of_work() as u:
            stop = next(o for o in u.orders.all() if o.purpose is OrderPurpose.ATTACHED_SL)
        monkeypatch.setattr(InMemoryUnitOfWork, "commit", fail)
        await sync.sender.amend(stop, trigger=Price(D(91)))
        await sync.sender.amend(stop, trigger=Price(D(92)))
        await sync.sender.cancel(stop)
        assert len(sync.journal.read()) == 6
        monkeypatch.setattr(InMemoryUnitOfWork, "commit", original)
        clock.advance(timedelta(seconds=1))
        await sync.synchronize()
        async with db.unit_of_work() as u:
            assert len([k for k, v in u.recovery.items() if k.startswith("journal:")]) == 6
        await sync.close()

    asyncio.run(scenario())


def test_d90_qty_only_amend_and_invalid_direction(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = setup(tmp_path)
        await start(sync)
        await sync.sender.send(await admit(router))
        bar(clock, ex)
        stop = next(o for o in await ex.get_open_orders(BTC) if o.parent_order_link_id)
        assert isinstance(await ex.amend_stop(stop.exchange_order_id, qty=Qty(D("-.5"))), Accepted)
        changed = next(o for o in await ex.get_open_orders(BTC) if o.parent_order_link_id)
        assert changed.order_qty.value == D("-.5") and changed.trigger_price == stop.trigger_price
        assert (
            await ex.amend_stop(stop.exchange_order_id, qty=Qty(D(".5")))
        ).code is ErrorCode.REJECTED
        assert (await ex.amend_stop(stop.exchange_order_id)).code is ErrorCode.REJECTED
        await sync.close()

    asyncio.run(scenario())


def record(op="one", phase="requested"):
    return JournalRecord(op, phase, 0, "cancel", "target", "{}", "{}", NOW.isoformat())


@pytest.mark.parametrize("tail", [b'{"cut":', b'{"cut":"\xe3\x81'])
def test_journal_truncated_tail_quarantined_and_sequence_continues(tmp_path, tail):
    journal = ProtectiveJournal(tmp_path / "한글 경로")
    journal.append(record())
    path = next(journal.directory.glob("journal-*.jsonl"))
    with path.open("ab") as stream:
        stream.write(tail)
    restored = ProtectiveJournal(journal.directory)
    assert restored.uncertain
    assert len(restored.read()) == 1
    assert path.with_suffix(".partial").read_bytes() == tail
    assert restored.append(record("two")).seq == 2


def test_journal_interior_corruption_blocks_replay(tmp_path):
    journal = ProtectiveJournal(tmp_path)
    journal.append(record())
    path = next(tmp_path.glob("journal-*.jsonl"))
    path.write_bytes(b"broken\n" + path.read_bytes())
    restored = ProtectiveJournal(tmp_path)
    assert restored.corrupt
    with pytest.raises(JournalCorrupt):
        restored.read()


def test_journal_path_independent_of_cwd_and_archive(tmp_path, monkeypatch):
    location = tmp_path / "한글 경로"
    journal = ProtectiveJournal(location)
    first = journal.append(record())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    restored = ProtectiveJournal(location)
    restored.archive({first.key})
    assert len(list((location / "archive").glob("*.jsonl"))) == 1
    assert restored.append(record("next")).seq == 2
    assert not list(elsewhere.iterdir())


async def filled_account(tmp_path, faults=()):
    db, clock, svc, router, ex, sync = setup(tmp_path, faults)
    await start(sync)
    order = await admit(router)
    await sync.sender.send(order)
    bar(clock, ex)
    await sync.tick()
    clock.advance(timedelta(seconds=1))
    await sync.tick()
    return db, clock, svc, router, ex, sync


@pytest.mark.parametrize("fault", [F.DUPLICATE, F.ORDER_FIRST])
def test_duplicate_and_reversed_events_book_once(tmp_path, fault):
    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path, [Fault("fill", 1, fault)])
        assert (await book(db)).qty == 1
        assert (await book(db)).fees == D(".2")
        clock.advance(timedelta(seconds=1))
        assert await sync.synchronize()
        assert (await book(db)).qty == 1
        await sync.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [F.PARTIAL_FILL, F.REJECT])
def test_close_ioc_retries_with_stops_retained(tmp_path, failure):
    async def scenario():
        # first fill is entry; the second is the close
        db, clock, svc, router, ex, sync = await filled_account(
            tmp_path, [Fault("fill", 2, failure, value=D(".5")), Fault("fill", 2, F.ORDER_FIRST)]
        )
        await router.handle(signal(kind="EXIT", bar=clock.now()), market(), RISK_INPUT)
        await sync.tick()
        bar(clock, ex)
        await sync.tick()
        held = (await book(db)).qty
        assert held == (D(".5") if failure is F.PARTIAL_FILL else D(1))
        assert sum(s.qty for s in (await book(db)).stops) == held
        clock.advance(timedelta(seconds=6))
        await sync.tick()
        bar(clock, ex)
        await sync.tick()
        assert (await book(db)).qty == 0
        assert sum(s.qty for s in (await book(db)).stops) == 0
        await sync.close()

    asyncio.run(scenario())


def test_disconnected_stop_fill_recovers_without_reentry(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        ex.disconnect()
        await asyncio.sleep(0)
        assert router.gate.pauses.blocking(signal().strategy, BTC)
        bar(clock, ex, low="89")
        ex.reconnect()
        assert not await sync.synchronize()
        clock.advance(timedelta(seconds=1))
        await sync.synchronize()
        assert (await book(db)).qty == 0
        assert len(await ex.get_executions(BTC, NOW)) == 2
        await sync.close()

    asyncio.run(scenario())


def test_rest_snapshot_stop_race_invalidates_confirmation(tmp_path, monkeypatch):
    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        original = ex.get_open_orders
        triggered = False

        async def racing(symbol):
            nonlocal triggered
            result = await original(symbol)
            if not triggered:
                triggered = True
                bar(clock, ex, low="89")
                await asyncio.sleep(0)
            return result

        monkeypatch.setattr(ex, "get_open_orders", racing)
        assert not await sync.synchronize()
        assert sync.pauses.blocking(signal().strategy, BTC)
        clock.advance(timedelta(seconds=1))
        await sync.synchronize()
        assert (await book(db)).qty == 0
        await sync.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("lost", [False, True])
def test_unjournaled_emergency_close_is_adopted_after_restart(tmp_path, monkeypatch, lost):
    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(
            tmp_path, [Fault("place", 2, F.RESPONSE_LOST)] if lost else ()
        )
        await router.handle(signal(kind="EXIT", bar=clock.now()), market(), RISK_INPUT)
        async with db.unit_of_work() as u:
            order = next(o for o in u.orders.all() if o.purpose is OrderPurpose.CLOSE)
        original = InMemoryUnitOfWork.commit

        async def fail(self):
            raise OSError("db down")

        def disk_fail(record):
            segment = tmp_path / "journal-00000001.jsonl"
            with segment.open("ab") as stream:
                stream.write(b'{"operation_id":')
            raise OSError("partial disk write")

        monkeypatch.setattr(InMemoryUnitOfWork, "commit", fail)
        monkeypatch.setattr(sync.journal, "append", disk_fail)
        result = await sync.sender.send(order)
        assert result.code is ErrorCode.TIMEOUT if lost else isinstance(result, Accepted)
        assert sync.journal.read() == ()
        await sync.close()
        monkeypatch.setattr(InMemoryUnitOfWork, "commit", original)
        fresh = Synchronizer(
            db.unit_of_work,
            ex,
            svc,
            ProtectiveJournal(tmp_path),
            clock,
            router.gate.pauses,
            sync.sender.unique,
            POLICY,
            (BTC,),
        )
        clock.advance(timedelta(seconds=1))
        await fresh.synchronize()
        open_closes = [
            o for o in await ex.get_open_orders(BTC) if o.reduce_only and not o.trigger_price
        ]
        assert len(open_closes) == 1 and open_closes[0].order_link_id == order.order_link_id
        bar(clock, ex)
        await fresh.tick()
        assert (await book(db)).qty == 0
        assert (
            len(
                [
                    o
                    for o in await ex.get_order_history(BTC, NOW)
                    if o.order_link_id == order.order_link_id
                ]
            )
            == 1
        )
        await fresh.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["APPLIED", "NOT_APPLIED", "SUPERSEDED", "TERMINAL"])
def test_requested_amend_replay_is_classification_only(tmp_path, mode):
    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        async with db.unit_of_work() as u:
            stop = next(o for o in u.orders.all() if o.purpose is OrderPurpose.ATTACHED_SL)
        target = stop.exchange_order_id
        payload = {
            "symbol": BTC.value,
            "fields": ["qty"],
            "before": {"qty": "-1", "cumulative": "0"},
            "after": {"qty": "-.5"},
        }
        requested = JournalRecord(
            "lost-amend",
            "requested",
            0,
            "amend",
            target,
            json.dumps(payload),
            "{}",
            clock.now().isoformat(),
        )
        sync.journal.append(requested)
        if mode == "APPLIED":
            await ex.amend_stop(target, qty=Qty(D("-.5")))
        elif mode == "SUPERSEDED":
            await ex.amend_stop(target, qty=Qty(D("-.3")))
        elif mode == "TERMINAL":
            await ex.cancel_order(target)
        # Isolate reconciliation: no outbox dispatcher should re-send the old amend.
        records = await sync._load_journal()
        await sync._refresh(BTC, NOW)
        await sync._replay_outcomes(records)
        async with db.unit_of_work() as u:
            assert u.recovery.get("outcome:lost-amend") == (
                "SUPERSEDED" if mode == "TERMINAL" else mode
            )
        await sync._replay_outcomes(records)
        actual = next(
            o for o in await ex.get_order_history(BTC, NOW) if o.exchange_order_id == target
        )
        assert actual.order_qty.value == D(
            {"APPLIED": "-.5", "NOT_APPLIED": "-1", "SUPERSEDED": "-.3", "TERMINAL": "-1"}[mode]
        )
        await sync.close()

    asyncio.run(scenario())


def test_history_out_of_range_and_manual_pause_survive_restart(tmp_path):
    async def scenario():
        from richletsgo.risk.pause import Pause, PauseKind

        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        sync.pauses.add(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "operator", clock.now()))
        await sync.tick()
        await sync.close()
        clock.advance(timedelta(days=8))
        fresh = Synchronizer(
            db.unit_of_work,
            ex,
            svc,
            ProtectiveJournal(tmp_path),
            clock,
            router.gate.pauses,
            sync.sender.unique,
            POLICY,
            (BTC,),
        )
        assert not await fresh.synchronize()
        assert fresh.pauses.has(PauseKind.ENTRY_PAUSED_MANUAL)
        assert "history_out_of_range" in fresh.alerts
        await fresh.close()

    asyncio.run(scenario())


def test_journal_fsync_failure_still_sends_and_marks_uncertain(tmp_path, monkeypatch):
    async def scenario():
        import os

        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        async with db.unit_of_work() as u:
            stop = next(o for o in u.orders.all() if o.purpose is OrderPurpose.ATTACHED_SL)

        def fail(fd):
            raise OSError("fsync unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail)
            assert isinstance(await sync.sender.amend(stop, trigger=Price(D(91))), Accepted)
        assert sync.journal.uncertain
        assert (tmp_path / "JOURNAL_UNCERTAIN").exists()
        events = await ex.get_open_orders(BTC)
        assert any(o.trigger_price == Price(D(91)) for o in events)
        restored = ProtectiveJournal(tmp_path)
        assert len(restored.read()) == 2
        await sync.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_signed_entry_exit_and_no_atomic_reversal(tmp_path, direction):
    async def scenario():
        from richletsgo.risk.records import Direction

        db, clock, svc, router, ex, sync = setup(tmp_path)
        await start(sync)
        sig = signal(direction=Direction(direction))
        result = await router.handle(sig, market(), RISK_INPUT)
        await sync.submit(result.order)
        bar(clock, ex)
        await sync.tick()
        sign = 1 if direction == "LONG" else -1
        assert (await book(db)).qty == sign
        opposite = Direction.SHORT if sign == 1 else Direction.LONG
        clock.advance(timedelta(seconds=1))
        admission = await router.handle(
            signal(direction=opposite, bar=clock.now()), market(), RISK_INPUT
        )
        assert admission.order is None
        await sync.tick()
        bar(clock, ex)
        await sync.tick()
        assert (await book(db)).qty == 0
        await sync.close()

    asyncio.run(scenario())


def test_same_signal_and_ai_reduction_do_not_top_up(tmp_path):
    async def scenario():
        from richletsgo.portfolio.signals import AiMode, AiScore, ScoreStatus

        db, clock, svc, router, ex, sync = setup(tmp_path)
        await start(sync)
        router.mode = AiMode.ENABLED
        sig = signal()
        router.scores.put(sig.signal_id, AiScore(ScoreStatus.OK, D(".5")))
        a, b = await asyncio.gather(*(router.handle(sig, market(), RISK_INPUT) for _ in range(2)))
        assert a.order.key == b.order.key
        await sync.submit(a.order)
        await sync.submit(b.order)
        bar(clock, ex)
        await sync.tick()
        later = signal(bar=clock.now())
        router.scores.put(later.signal_id, AiScore(ScoreStatus.OK, D(1)))
        result = await router.handle(later, market(), RISK_INPUT)
        assert result.order is None
        assert (await book(db)).qty == D(".5")
        assert len([o for o in await ex.get_order_history(BTC, NOW) if not o.reduce_only]) == 1
        await sync.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["TIMEOUT", "INVALID_FORMAT", "BUDGET_EXCEEDED"])
def test_ai_failure_blocks_entry_but_exit_still_sends(tmp_path, status):
    async def scenario():
        from richletsgo.portfolio.signals import AiMode, AiScore, ScoreStatus

        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        router.mode = AiMode.ENABLED
        sig = signal(
            strategy=StrategyId.S2,
            bar=clock.now(),
        )
        router.scores.put(sig.signal_id, AiScore(ScoreStatus(status)))
        assert (await router.handle(sig, market(), RISK_INPUT)).order is None
        await router.handle(signal(kind="EXIT", bar=clock.now()), market(), RISK_INPUT)
        await sync.tick()
        bar(clock, ex)
        await sync.tick()
        assert (await book(db)).qty == 0
        await sync.close()

    asyncio.run(scenario())


def test_foreign_reduction_is_attributed_and_pauses_contract(tmp_path):
    async def scenario():
        from richletsgo.exchange.contracts import OrderRequest

        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        await ex.place_order(
            OrderRequest("external-manual", BTC, Qty(D("-.5")), OrderPurpose.CLOSE, Price(D(100)))
        )
        bar(clock, ex)
        await sync.tick()
        clock.advance(timedelta(seconds=1))
        await sync.tick()
        async with db.unit_of_work() as u:
            assert u.portfolio.paused(BTC)
        assert any("external/overflow" in text for text in sync.alerts)
        assert (await book(db)).qty <= D(".5")
        await sync.close()

    asyncio.run(scenario())


def test_close_intent_survives_hours_offline_with_exchange_stop(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        await router.handle(signal(kind="EXIT", bar=clock.now()), market(), RISK_INPUT)
        await sync.close()
        clock.advance(timedelta(hours=4))
        bar(clock, ex, low="89")
        fresh = Synchronizer(
            db.unit_of_work,
            ex,
            svc,
            ProtectiveJournal(tmp_path),
            clock,
            router.gate.pauses,
            sync.sender.unique,
            POLICY,
            (BTC,),
        )
        await fresh.synchronize()
        clock.advance(timedelta(seconds=1))
        await fresh.synchronize()
        assert (await book(db)).qty == 0
        assert not [o for o in await ex.get_open_orders(BTC) if not o.trigger_price]
        await fresh.close()

    asyncio.run(scenario())


def test_missing_stop_restoration_and_rejected_protection_reduces(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(
            tmp_path, [Fault("place", 2, F.REJECT), Fault("place", 3, F.REJECT)]
        )
        stop = next(o for o in await ex.get_open_orders(BTC) if o.trigger_price)
        await ex.cancel_order(stop.exchange_order_id)
        for _ in range(5):
            clock.advance(timedelta(seconds=6))
            await sync.tick()
        # Bounded protection retries produce a reduce-only order, not an entry.
        opened = await ex.get_open_orders(BTC)
        assert any(o.reduce_only and not o.trigger_price for o in opened)
        bar(clock, ex)
        await sync.tick()
        assert (await book(db)).qty == 0
        assert any("exhausted" in a for a in sync.alerts)
        await sync.close()

    asyncio.run(scenario())


def test_journal_rejects_entry_bypass(tmp_path):
    with pytest.raises(ValueError, match="entry"):
        JournalRecord(
            "entry",
            "requested",
            0,
            "create",
            "entry",
            '{"reduce_only":false}',
            "{}",
            NOW.isoformat(),
        )


def test_two_symbols_share_atomic_account_limit(tmp_path):
    async def scenario():
        from richletsgo.domain import Symbol

        db, clock, svc, router, ex, sync = setup(tmp_path)
        await start(sync)
        router.gate.limits = replace(router.gate.limits, max_account_notional_usdt=D(150))
        btc = signal()
        eth = replace(btc, symbol=Symbol.ETHUSDT)
        results = await asyncio.gather(
            router.handle(btc, market(), RISK_INPUT),
            router.handle(eth, market(Symbol.ETHUSDT), RISK_INPUT),
        )
        admitted = [r for r in results if r.order is not None]
        assert len(admitted) == 1
        assert admitted[0].order.symbol is BTC
        await sync.submit(admitted[0].order)
        assert len(await ex.get_open_orders(BTC)) == 1
        await sync.close()

    asyncio.run(scenario())


def test_order_before_execution_reserves_until_booked(tmp_path, monkeypatch):
    async def scenario():
        from richletsgo.domain import Symbol
        from richletsgo.oms.exposure import exposure

        db, clock, svc, router, ex, sync = setup(tmp_path, [Fault("fill", 1, F.ORDER_FIRST)])
        await start(sync)
        router.gate.limits = replace(router.gate.limits, max_account_notional_usdt=D(150))
        await sync.submit(await admit(router))
        seen = []
        original = svc.on_execution

        async def delayed(event):
            async with db.unit_of_work() as u:
                view = exposure(u, marks={BTC: Price(D(100))}, slippage_buffer=D(0))
                seen.append(view.total_notional)
                assert view.total_notional >= 100
            other = replace(signal(bar=clock.now()), symbol=Symbol.ETHUSDT)
            assert (await router.handle(other, market(Symbol.ETHUSDT), RISK_INPUT)).order is None
            return await original(event)

        monkeypatch.setattr(svc, "on_execution", delayed)
        bar(clock, ex)
        await sync.tick()
        assert seen and (await book(db)).qty == 1
        await sync.close()

    asyncio.run(scenario())


def test_late_execution_times_out_then_rest_restores(tmp_path, monkeypatch):
    async def scenario():
        from richletsgo.domain import ExecutionEvent

        db, clock, svc, router, ex, sync = setup(tmp_path)
        await start(sync)
        await sync.sender.send(await admit(router))
        # Exchange evolves without a stream consumer; order REST is visible but executions lag.
        await sync.close()
        ex.drain_events()
        bar(clock, ex)
        events = ex.drain_events()
        for event in events:
            if isinstance(event, ExecutionEvent):
                continue
            sync._buffer.append(event)
        original = ex.get_executions

        async def absent(symbol, since):
            return ()

        monkeypatch.setattr(ex, "get_executions", absent)
        await sync.tick()
        clock.advance(timedelta(seconds=21))
        await sync.tick()
        assert any(p.reason == "unbooked" for p in sync.pauses.active())
        assert (await book(db)).qty == 0
        monkeypatch.setattr(ex, "get_executions", original)
        await sync.tick()
        assert (await book(db)).qty == 1

    asyncio.run(scenario())


def test_submitted_before_send_restart_never_resends(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = setup(tmp_path)
        await start(sync)
        order = await admit(router)
        async with db.unit_of_work() as u:
            u.orders.put(replace(order, state=OrderState.SUBMITTED))
            await u.commit()
        await sync.close()
        fresh = Synchronizer(
            db.unit_of_work,
            ex,
            svc,
            ProtectiveJournal(tmp_path),
            clock,
            router.gate.pauses,
            sync.sender.unique,
            POLICY,
            (BTC,),
        )
        clock.advance(timedelta(seconds=1))
        assert not await fresh.synchronize()
        assert await ex.get_order_history(BTC, NOW) == ()
        async with db.unit_of_work() as u:
            assert u.orders.get(order.key).state is OrderState.UNKNOWN
            assert u.orders.get(order.key).reserved_qty == 1
        await fresh.close()

    asyncio.run(scenario())


def test_cancel_race_filled_twice_books_once(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = setup(tmp_path, [Fault("cancel", 1, F.CANCEL_RACE)])
        await start(sync)
        # Establish a market mark without a position.
        bar(clock, ex)
        order = await router.handle(signal(bar=clock.now()), market(), RISK_INPUT)
        await sync.submit(order.order)
        await sync.sender.cancel(order.order)
        await sync.tick()
        assert (await book(db)).qty == 1
        assert (await book(db)).fees == D(".2")
        await sync.close()

    asyncio.run(scenario())


def test_execution_before_place_response_is_not_overwritten(tmp_path, monkeypatch):
    async def scenario():
        db, clock, svc, router, ex, sync = setup(tmp_path)
        await start(sync)
        original = ex.place_order

        async def immediate(request):
            result = await original(request)
            bar(clock, ex)
            # Emulates WS processing while an HTTP response is still pending.
            for event in ex.drain_events():
                from richletsgo.domain import ExecutionEvent, OrderEvent

                if isinstance(event, OrderEvent):
                    await svc.on_order(event, reference_price=Price(D(100)))
                elif isinstance(event, ExecutionEvent):
                    await svc.on_execution(event)
            return result

        monkeypatch.setattr(ex, "place_order", immediate)
        order = await admit(router)
        await sync.sender.send(order)
        async with db.unit_of_work() as u:
            assert u.orders.get(order.key).state is OrderState.FILLED
        assert (await book(db)).qty == 1
        await sync.close()

    asyncio.run(scenario())


def test_manual_absence_late_fill_corrected_through_gateway(tmp_path):
    async def scenario():
        from richletsgo.oms.models import confirm_not_found

        db, clock, svc, router, ex, sync = setup(tmp_path, [Fault("place", 1, F.RESPONSE_LOST)])
        await start(sync)
        order = await admit(router)
        await sync.sender.send(order)
        async with db.unit_of_work() as u:
            current = u.orders.get(order.key)
            corrected, audit = confirm_not_found(current, "operator", clock.now())
            u.orders.put(corrected)
            u.recovery.put("manual_resolution", audit.actor)
            await u.commit()
        bar(clock, ex)
        await sync.tick()
        assert (await book(db)).qty == 1
        async with db.unit_of_work() as u:
            assert u.orders.get(order.key).state is OrderState.FILLED
        await sync.close()

    asyncio.run(scenario())


def test_data_gap_policy_blocks_entry_and_releases_after_backfill(tmp_path):
    async def scenario():
        from richletsgo.risk.guards import assess_market_data
        from richletsgo.risk.pause import Pause, PauseKind

        db, clock, svc, router, ex, sync = setup(tmp_path)
        await start(sync)
        status = assess_market_data(
            BTC,
            interval=timedelta(hours=1),
            last_closed_bar_start=NOW - timedelta(hours=3),
            now=NOW,
            allowed_delay=timedelta(0),
        )
        assert status.stale
        sync.pauses.add(Pause(PauseKind.ENTRY_PAUSED_SYSTEM, "data", NOW, symbol=BTC))
        assert (await router.handle(signal(), market(), RISK_INPUT)).order is None
        status = assess_market_data(
            BTC,
            interval=timedelta(hours=1),
            last_closed_bar_start=NOW - timedelta(hours=1),
            now=NOW,
            allowed_delay=timedelta(0),
        )
        assert not status.stale
        sync.pauses.clear(PauseKind.ENTRY_PAUSED_SYSTEM, reason="data", symbol=BTC)
        result = await router.handle(signal(bar=NOW + timedelta(hours=1)), market(), RISK_INPUT)
        assert isinstance(await sync.submit(result.order), Accepted)
        await sync.close()

    asyncio.run(scenario())


def test_risk_halt_resume_authorization_and_midnight(tmp_path):
    async def scenario():
        from richletsgo.risk.equity import AccountSnapshot
        from richletsgo.risk.pause import PauseKind
        from richletsgo.risk.resume import ResumeRejected, ResumeService

        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        router.gate.tracker.observe(AccountSnapshot(clock.now(), D(800), D(0)))
        await router.handle(signal(strategy=StrategyId.S2, bar=clock.now()), market(), RISK_INPUT)
        assert sync.pauses.has(PauseKind.HALTED)
        resume = ResumeService(
            pauses=sync.pauses,
            tracker=router.gate.tracker,
            limits=router.gate.limits,
            allowed_ids={"owner"},
            ttl=timedelta(minutes=1),
            id_factory=counter(),
        )
        refused = resume.request_resume("stranger", clock.now())
        assert isinstance(refused, ResumeRejected)
        async with db.unit_of_work() as u:
            u.recovery.put("command:stranger", refused.reason)
            await u.commit()
        prompt = resume.request_resume("owner", clock.now())
        record = resume.confirm_resume(prompt.prompt_id, "owner", clock.now())
        assert record.peak_reset.new_risk_peak == 800
        assert not sync.pauses.has(PauseKind.HALTED)
        # Daily halt remains until the day boundary; funding precedes start_day in caller.
        next_day = (clock.now() + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        clock.set(next_day)
        router.gate.tracker.start_day(AccountSnapshot(next_day, D(800), D(0)))
        sync.pauses.expire(next_day)
        assert not sync.pauses.has(PauseKind.ENTRY_PAUSED_DAILY)
        await sync.tick()
        await sync.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["newest", "oldest", "preferred_parent"])
def test_replacement_auto_shrink_never_cancels_remaining_protection(tmp_path, mode):
    from richletsgo.exchange.simulated.engine import StopReduction
    from richletsgo.portfolio.models import Action, ActionKind

    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        ex.reduction = StopReduction(mode)
        async with db.unit_of_work() as u:
            stop = next(o for o in u.orders.all() if o.purpose is OrderPurpose.ATTACHED_SL)
        # Inject an oversized exchange stop as the starting recovery condition.
        # Subsequent creates and automatic shrink use the real exchange engine.
        remote = ex._orders[stop.exchange_order_id]
        remote.request = replace(remote.request, qty=Qty(D(-2)))
        ex.preferred_parent = remote.parent
        clock.advance(timedelta(seconds=1))
        await sync._refresh(BTC, NOW)
        async with db.unit_of_work() as u:
            stop = u.orders.get(stop.key)
        action = Action("replace-test", ActionKind.TRIM_STOP, stop.strategy, BTC, D(1), stop.key)
        await sync._replacement(action, stop, NOW)
        await sync._drain()
        clock.advance(timedelta(seconds=1))
        await sync._replacement(action, stop, NOW)
        current = await book(db)
        assert sum(s.qty for s in current.stops) == abs(current.qty) == 1
        records = sync.journal.read()
        assert not any(r.step == 3 for r in records)
        assert len([r for r in records if r.phase == "requested" and r.step == 1]) == 1
        await sync.close()

    asyncio.run(scenario())


def test_trim_remaining_converts_to_signed_total_and_zero_cancels(tmp_path):
    from richletsgo.portfolio.models import Action, ActionKind

    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        async with db.unit_of_work() as u:
            stop = next(o for o in u.orders.all() if o.purpose is OrderPurpose.ATTACHED_SL)
            stop = replace(stop, qty=Qty(D(-2)), exchange_cum_exec_qty=D(1), booked_qty=Qty(D(-1)))
            u.orders.put(stop)
            u.portfolio.emit(
                Action("trim-half", ActionKind.TRIM_STOP, stop.strategy, BTC, D(".5"), stop.key)
            )
            await u.commit()
        remote = ex._orders[stop.exchange_order_id]
        remote.request = replace(remote.request, qty=Qty(D(-2)))
        remote.cumulative = D(1)
        await sync._actions(NOW)
        request = next(
            r for r in sync.journal.read() if r.action_key == "trim-half" and r.phase == "requested"
        )
        assert request.payload()["before"]["cumulative"] == "1"
        assert D(request.payload()["after"]["qty"]) == D("-1.5")
        assert remote.request.qty.value == D("-1.5")
        async with db.unit_of_work() as u:
            u.portfolio.emit(
                Action("trim-zero", ActionKind.TRIM_STOP, stop.strategy, BTC, D(0), stop.key)
            )
            await u.commit()
        await sync._actions(NOW)
        assert remote.state is OrderState.CANCELLED
        assert any(
            r.action_key == "trim-zero" and r.op_type == "cancel" for r in sync.journal.read()
        )
        await sync.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("crash", ["none", "before_cancel", "after_cancel"])
def test_replacement_create_before_cancel_restart_group_and_cancel_retry(
    tmp_path, monkeypatch, crash
):
    from richletsgo.portfolio.models import Action, ActionKind

    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(
            tmp_path, (Fault("amend", 1, F.AMEND_UNSUPPORTED),)
        )
        async with db.unit_of_work() as u:
            stop = next(o for o in u.orders.all() if o.purpose is OrderPurpose.ATTACHED_SL)
        remote = ex._orders[stop.exchange_order_id]
        remote.request = replace(remote.request, qty=Qty(D(-2)))
        # A venue without automatic stop resizing exercises D90's cancel branch.
        monkeypatch.setattr(ex, "_shrink_stops", lambda symbol, at: None)
        clock.advance(timedelta(seconds=1))
        await sync._refresh(BTC, NOW)
        async with db.unit_of_work() as u:
            stop = u.orders.get(stop.key)
            u.portfolio.emit(
                Action("unsupported", ActionKind.TRIM_STOP, stop.strategy, BTC, D(1), stop.key)
            )
            await u.commit()
        await sync._actions(NOW)
        assert remote.state is OrderState.OPEN  # new accepted, not yet snapshot-confirmed
        records = sync.journal.read()
        assert len([r for r in records if r.step == 1 and r.phase == "requested"]) == 1
        unique = sync.sender.unique
        await sync.close()
        fresh = Synchronizer(
            db.unit_of_work,
            ex,
            svc,
            ProtectiveJournal(tmp_path),
            clock,
            router.gate.pauses,
            unique,
            POLICY,
            (BTC,),
        )
        clock.advance(timedelta(seconds=1))
        original_cancel = ex.cancel_order

        async def interrupted(target):
            if crash == "after_cancel":
                await original_cancel(target)
            raise RuntimeError("process terminated after requested journal")

        if crash != "none":
            monkeypatch.setattr(ex, "cancel_order", interrupted)
            with pytest.raises(RuntimeError):
                await fresh.synchronize()
            await fresh.close()
            monkeypatch.setattr(ex, "cancel_order", original_cancel)
            fresh = Synchronizer(
                db.unit_of_work,
                ex,
                svc,
                ProtectiveJournal(tmp_path),
                clock,
                router.gate.pauses,
                unique,
                POLICY,
                (BTC,),
            )
        else:
            await fresh.synchronize()
        clock.advance(timedelta(seconds=6))
        await fresh.synchronize()
        clock.advance(timedelta(seconds=1))
        assert await fresh.synchronize()
        records = fresh.journal.read()
        creates = [r for r in records if r.step == 1 and r.phase == "requested"]
        cancels = [r for r in records if r.step == 3 and r.phase == "requested"]
        assert len(creates) == 1
        assert len(cancels) == (2 if crash == "before_cancel" else 1)
        assert creates[0].group_id == cancels[0].group_id
        assert creates[0].operation_id != cancels[0].operation_id
        assert remote.state is OrderState.CANCELLED
        assert sum(s.qty for s in (await book(db)).stops) == 1
        await fresh.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("lost", [False, True])
def test_replacement_rejections_are_bounded_and_unknown_keeps_old(tmp_path, monkeypatch, lost):
    from richletsgo.portfolio.models import Action, ActionKind

    async def scenario():
        faults = (
            [Fault("place", 2, F.RESPONSE_LOST), Fault("place", 2, F.HIDE_ORDER, value=D(100))]
            if lost
            else [Fault("place", 2, F.REJECT), Fault("place", 3, F.REJECT)]
        )
        db, clock, svc, router, ex, sync = await filled_account(tmp_path, faults)
        async with db.unit_of_work() as u:
            stop = next(o for o in u.orders.all() if o.purpose is OrderPurpose.ATTACHED_SL)
        remote = ex._orders[stop.exchange_order_id]
        remote.request = replace(remote.request, qty=Qty(D(-2)))
        monkeypatch.setattr(ex, "_shrink_stops", lambda symbol, at: None)
        await sync._refresh(BTC, NOW)
        async with db.unit_of_work() as u:
            stop = u.orders.get(stop.key)
        action = Action("bounded", ActionKind.TRIM_STOP, stop.strategy, BTC, D(1), stop.key)
        if lost:
            ex.disconnect()
        for _ in range(5):
            clock.advance(timedelta(seconds=6))
            await sync._replacement(action, stop, NOW)
        assert remote.state is OrderState.OPEN and remote.remaining == 2
        records = sync.journal.read()
        assert len([r for r in records if r.step == 1 and r.phase == "requested"]) == (
            1 if lost else 2
        )
        assert not any(r.step == 3 for r in records)
        if not lost:
            assert any(p.reason == "replacement_exhausted" for p in sync.pauses.active())
        await sync.close()

    asyncio.run(scenario())


def test_empty_successful_lookups_preserve_unknown_until_late_open(tmp_path):
    async def scenario():
        faults = [
            Fault("place", 1, F.RESPONSE_LOST),
            Fault("place", 1, F.HIDE_ORDER, value=D(6)),
            Fault("cancel", 1, F.CANCEL_NOT_FOUND),
        ]
        db, clock, svc, router, ex, sync = setup(tmp_path, faults)
        await start(sync)
        order = await admit(router)
        ex.disconnect()
        await sync.sender.send(order)
        for _ in range(3):
            await sync.tick()
            async with db.unit_of_work() as u:
                current = u.orders.get(order.key)
                assert current.state is OrderState.UNKNOWN and current.reserved_qty == 1
            assert sync.pauses.blocking(order.strategy, BTC)
        await sync.tick()
        async with db.unit_of_work() as u:
            current = u.orders.get(order.key)
            assert current.state is OrderState.OPEN and current.reserved_qty == 1
        await sync.close()

    asyncio.run(scenario())


def test_unknown_partial_entry_then_cancel_retains_fill_and_attached_stop(tmp_path):
    async def scenario():
        faults = [
            Fault("place", 1, F.RESPONSE_LOST),
            Fault("fill", 1, F.PARTIAL_FILL, value=D(".5")),
        ]
        db, clock, svc, router, ex, sync = setup(tmp_path, faults)
        await start(sync)
        order = await admit(router)
        order = replace(order, limit_price=Price(D(100)))
        async with db.unit_of_work() as u:
            u.orders.put(order)
            await u.commit()
        ex.disconnect()
        await sync.sender.send(order)
        # Inject a maker execution before the one-bar GTC expiry boundary.
        ex._fill(ex._orders["sim-order-1"], Price(D(100)), clock.now(), maker=True)
        await sync._refresh(BTC, NOW)
        async with db.unit_of_work() as u:
            current = u.orders.get(order.key)
            assert current.state is OrderState.PARTIALLY_FILLED and current.reserved_qty == D(".5")
        await sync.sender.cancel(current)
        await sync._refresh(BTC, NOW)
        async with db.unit_of_work() as u:
            current = u.orders.get(order.key)
            assert current.state is OrderState.CANCELLED and current.reserved_qty == 0
        assert (await book(db)).qty == D(".5")
        assert sum(s.qty for s in (await book(db)).stops) == D(".5")
        await sync.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("external", [False, True])
def test_two_strategy_reduction_allocates_overflow_s2_first(tmp_path, monkeypatch, external):
    from richletsgo.exchange.contracts import OrderRequest

    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        result = await router.handle(
            signal(strategy=StrategyId.S2, bar=clock.now()), market(), RISK_INPUT
        )
        assert isinstance(await sync.submit(result.order), Accepted)
        bar(clock, ex)
        await sync.tick()
        async with db.unit_of_work() as u:
            assert u.portfolio.book(StrategyId.S2, BTC).qty == 1
            stop = next(
                o
                for o in u.orders.all()
                if o.purpose is OrderPurpose.ATTACHED_SL and o.strategy is StrategyId.S1
            )
        if external:
            await ex.place_order(
                OrderRequest("manual", BTC, Qty(D("-1.5")), OrderPurpose.CLOSE, Price(D(100)))
            )
            ex._fill(ex._orders["sim-order-5"], Price(D(100)), clock.now(), maker=False)
            expected = (D(".5"), D(0))
        else:
            # Both reduce-only instructions already reached matching before the
            # venue resized stops; model this race at the exchange execution boundary.
            monkeypatch.setattr(ex, "_shrink_stops", lambda symbol, at: None)
            await ex.place_order(
                OrderRequest("d-S1-X-race", BTC, Qty(D("-.5")), OrderPurpose.CLOSE, Price(D(100)))
            )
            ex._fill(ex._orders["sim-order-5"], Price(D(100)), clock.now(), maker=False)
            ex._fill(ex._orders[stop.exchange_order_id], Price(D(90)), clock.now(), maker=False)
            expected = (D(0), D(".5"))
        await sync.tick()
        clock.advance(timedelta(seconds=1))
        await sync.tick()
        async with db.unit_of_work() as u:
            assert (
                tuple(u.portfolio.book(s, BTC).qty for s in (StrategyId.S1, StrategyId.S2))
                == expected
            )
            if external:
                assert u.portfolio.paused(BTC)
            else:
                assert sync.alerts
        assert sum(p.qty.value for p in await ex.get_positions()) == sum(expected)
        await sync.close()

    asyncio.run(scenario())


def test_partial_entry_stopped_then_late_fill_stays_in_closing_episode(tmp_path):
    async def scenario():
        faults = [
            Fault("fill", 1, F.PARTIAL_FILL, value=D(".5")),
            Fault("cancel", 1, F.RESPONSE_LOST),
        ]
        db, clock, svc, router, ex, sync = setup(tmp_path, faults)
        await start(sync)
        order = replace(await admit(router), limit_price=Price(D(100)))
        async with db.unit_of_work() as u:
            u.orders.put(order)
            await u.commit()
        await sync.submit(order)
        remote = ex._orders["sim-order-1"]
        ex._fill(remote, Price(D(100)), clock.now(), maker=True)
        await sync.tick()
        ex._fill(ex._orders["sim-order-2"], Price(D(90)), clock.now(), maker=False)
        # Another match occurred before the cancel acknowledgement.
        ex._fill(remote, Price(D(100)), clock.now(), maker=True)
        await sync.tick()
        result = await router.handle(
            signal(bar=clock.now() + timedelta(hours=1)), market(), RISK_INPUT
        )
        assert result.order is None
        assert (await book(db)).qty == D(".5")
        assert sum(s.qty for s in (await book(db)).stops) == D(".5")
        bar(clock, ex)
        await sync.tick()
        assert (await book(db)).qty == 0
        assert len([o for o in await ex.get_order_history(BTC, NOW) if not o.reduce_only]) == 1
        await sync.close()

    asyncio.run(scenario())


def test_strategy_consecutive_loss_pause_survives_sync_and_expires(tmp_path):
    from test_risk import figures

    from richletsgo.risk.pause import PauseKind

    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        inputs = dict(RISK_INPUT)
        inputs[StrategyId.S2] = replace(figures(), consecutive_losses=3, last_loss_at=clock.now())
        result = await router.handle(
            signal(strategy=StrategyId.S2, bar=clock.now()), market(), inputs
        )
        assert result.order is None and sync.pauses.has(PauseKind.STRATEGY_PAUSED)
        await sync.synchronize()
        assert sync.pauses.has(PauseKind.STRATEGY_PAUSED)
        clock.advance(timedelta(hours=7))
        sync.pauses.expire(clock.now())
        assert not sync.pauses.has(PauseKind.STRATEGY_PAUSED)
        assert len(await ex.get_open_orders(BTC)) == 1  # attached stop remains
        await sync.close()

    asyncio.run(scenario())


def test_unknown_cancel_accepted_waits_for_terminal_evidence(tmp_path):
    async def scenario():
        faults = [Fault("place", 1, F.RESPONSE_LOST), Fault("place", 1, F.HIDE_ORDER, value=D(6))]
        db, clock, svc, router, ex, sync = setup(tmp_path, faults)
        await start(sync)
        order = await admit(router)
        ex.disconnect()
        await sync.sender.send(order)
        await sync.tick()
        async with db.unit_of_work() as u:
            assert u.orders.get(order.key).state is OrderState.UNKNOWN
            assert u.orders.get(order.key).reserved_qty == 1
        for _ in range(3):
            await sync.tick()
        async with db.unit_of_work() as u:
            assert u.orders.get(order.key).state is OrderState.CANCELLED
            assert u.orders.get(order.key).reserved_qty == 0
        await sync.close()

    asyncio.run(scenario())


def test_replacement_does_not_cancel_when_other_strategy_lost_protection(tmp_path):
    from richletsgo.exchange.simulated.engine import StopReduction
    from richletsgo.portfolio.models import Action, ActionKind

    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        added = await router.handle(
            signal(strategy=StrategyId.S2, bar=clock.now()), market(), RISK_INPUT
        )
        await sync.submit(added.order)
        bar(clock, ex)
        await sync.tick()
        async with db.unit_of_work() as u:
            stop = next(
                o
                for o in u.orders.all()
                if o.purpose is OrderPurpose.ATTACHED_SL and o.strategy is StrategyId.S1
            )
        ex._orders[stop.exchange_order_id].request = replace(
            ex._orders[stop.exchange_order_id].request, qty=Qty(D(-2))
        )
        ex.reduction = StopReduction.PREFERRED_PARENT
        ex.preferred_parent = added.order.order_link_id
        clock.advance(timedelta(seconds=1))
        await sync._refresh(BTC, NOW)
        async with db.unit_of_work() as u:
            stop = u.orders.get(stop.key)
        action = Action("other-owner", ActionKind.TRIM_STOP, stop.strategy, BTC, D(1), stop.key)
        await sync._replacement(action, stop, NOW)
        clock.advance(timedelta(seconds=1))
        await sync._replacement(action, stop, NOW)
        async with db.unit_of_work() as u:
            assert u.portfolio.book(StrategyId.S2, BTC).stops == ()
        assert not any(r.step == 3 for r in sync.journal.read())
        assert ex._orders[stop.exchange_order_id].state is OrderState.OPEN
        await sync.close()

    asyncio.run(scenario())


def test_replacement_equal_half_stops_are_both_retained(tmp_path):
    from richletsgo.exchange.contracts import OrderRequest
    from richletsgo.portfolio.models import Action, ActionKind

    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        async with db.unit_of_work() as u:
            stop = next(o for o in u.orders.all() if o.purpose is OrderPurpose.ATTACHED_SL)
        await ex.amend_stop(stop.exchange_order_id, qty=Qty(D("-.5")))
        await ex.place_order(
            OrderRequest(
                "d-S1-P-half",
                BTC,
                Qty(D("-.5")),
                OrderPurpose.PROTECT,
                Price(D(100)),
                trigger_price=Price(D(90)),
            )
        )
        clock.advance(timedelta(seconds=1))
        await sync._refresh(BTC, NOW)
        async with db.unit_of_work() as u:
            stop = u.orders.get(stop.key)
        action = Action("halves", ActionKind.TRIM_STOP, stop.strategy, BTC, D(".5"), stop.key)
        await sync._replacement(action, stop, NOW)
        assert len(await ex.get_open_orders(BTC)) == 2
        assert sorted(s.qty for s in (await book(db)).stops) == [D(".5"), D(".5")]
        assert not sync.journal.read()
        await sync.close()

    asyncio.run(scenario())


def test_long_holding_with_daily_reconciliation_restarts_and_closes(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        for _ in range(8):
            clock.advance(timedelta(days=1))
            await sync.tick()
            assert not any(p.reason == "history_out_of_range" for p in sync.pauses.active())
        async with db.unit_of_work() as u:
            assert u.recovery.get("reconciliation:" + BTC.value) == clock.now().isoformat()
        await sync.close()
        fresh = Synchronizer(
            db.unit_of_work,
            ex,
            svc,
            ProtectiveJournal(tmp_path),
            clock,
            router.gate.pauses,
            sync.sender.unique,
            POLICY,
            (BTC,),
        )
        clock.advance(timedelta(seconds=1))
        assert await fresh.synchronize()
        await router.handle(signal(kind="EXIT", bar=clock.now()), market(), RISK_INPUT)
        await fresh.tick()
        assert any(o.reduce_only and not o.trigger_price for o in await ex.get_open_orders(BTC))
        await fresh.close()

    asyncio.run(scenario())


def test_history_gap_keeps_pause_but_refreshes_and_sends_close(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        async with db.unit_of_work() as u:
            watermark = u.recovery.get("reconciliation:" + BTC.value)
        await sync.close()
        clock.advance(timedelta(days=8))
        fresh = Synchronizer(
            db.unit_of_work,
            ex,
            svc,
            ProtectiveJournal(tmp_path),
            clock,
            router.gate.pauses,
            sync.sender.unique,
            POLICY,
            (BTC,),
        )
        assert not await fresh.synchronize()
        await router.handle(signal(kind="EXIT", bar=clock.now()), market(), RISK_INPUT)
        await fresh.tick()
        assert any(o.reduce_only and not o.trigger_price for o in await ex.get_open_orders(BTC))
        async with db.unit_of_work() as u:
            assert u.recovery.get("reconciliation:" + BTC.value) == watermark
            assert u.recovery.get("history_gap:" + BTC.value) == "unresolved"
        assert "history_out_of_range" in fresh.alerts
        assert not await fresh.synchronize()
        await fresh.close()

    asyncio.run(scenario())


def test_reconciliation_watermark_uses_query_start_and_ignores_failed_round(tmp_path, monkeypatch):
    from richletsgo.exchange.contracts import ExchangeError

    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        original = ex.get_executions
        async with db.unit_of_work() as u:
            previous = u.recovery.get("reconciliation:" + BTC.value)

        async def failed(symbol, since):
            return ExchangeError(ErrorCode.TIMEOUT, "query failed")

        monkeypatch.setattr(ex, "get_executions", failed)
        clock.advance(timedelta(seconds=10))
        await sync._refresh(BTC, NOW)
        async with db.unit_of_work() as u:
            assert u.recovery.get("reconciliation:" + BTC.value) == previous
        started = clock.now()

        async def slow(symbol, since):
            clock.advance(timedelta(seconds=10))
            return await original(symbol, since)

        monkeypatch.setattr(ex, "get_executions", slow)
        await sync._refresh(BTC, NOW)
        async with db.unit_of_work() as u:
            assert u.recovery.get("reconciliation:" + BTC.value) == started.isoformat()
        assert await sync._since() == started - POLICY.overlap
        await sync.close()

    asyncio.run(scenario())


def test_reconciliation_uses_oldest_contract_watermark(tmp_path):
    from richletsgo.domain import Symbol

    async def scenario():
        db, clock, svc, router, ex, sync = await filled_account(tmp_path)
        sync.symbols = (BTC, Symbol.ETHUSDT)
        old = clock.now() - timedelta(days=2)
        async with db.unit_of_work() as u:
            u.recovery.put("reconciliation:ETHUSDT", old.isoformat())
            await u.commit()
        await sync._refresh(BTC, NOW)
        assert await sync._since() == old - POLICY.overlap
        await sync.close()

    asyncio.run(scenario())
