"""Offline state-machine, transaction and attribution regressions for M1-02."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from richletsgo.domain import (
    Environment,
    ExecutionEvent,
    OrderEvent,
    OrderPurpose,
    OrderState,
    OrderType,
    Price,
    Qty,
    StopKind,
    StrategyId,
    Symbol,
    TimeInForce,
)
from richletsgo.oms.exposure import exposure, unbooked_work
from richletsgo.oms.fills import (
    Attribution,
    FillResult,
    apply_execution,
    ingest_order_event,
    pending_work,
)
from richletsgo.oms.models import OrderRecord, Transition, apply_order_event, confirm_not_found
from richletsgo.oms.recovery import (
    CancelResult,
    LookupResult,
    RecoveryAction,
    resolve_unknown,
)
from richletsgo.storage.contracts import FillStatus, UnitOfWork
from richletsgo.storage.memory import InMemoryDatabase, InMemoryUnitOfWork

NOW = datetime(2026, 10, 20, tzinfo=UTC)
BTC = Symbol.BTCUSDT


def order(
    *,
    strategy=StrategyId.S1,
    purpose=OrderPurpose.ENTRY,
    qty="1",
    state=OrderState.SUBMITTED,
    symbol=BTC,
    suffix="one",
):
    link = (
        f"d-{strategy.value}-{purpose.value}-{suffix}"
        if purpose is not OrderPurpose.ATTACHED_SL
        else None
    )
    return OrderRecord(
        link,
        f"exchange-{suffix}",
        None,
        strategy,
        purpose,
        symbol,
        Qty(D(qty)),
        None,
        Price(D(100)),
        state,
        Qty(D(0)),
        D(0),
        NOW,
        NOW,
    )


def event(record, *, state=OrderState.OPEN, cumulative="0", at=NOW):
    is_stop = record.purpose in (OrderPurpose.ATTACHED_SL, OrderPurpose.PROTECT)
    return OrderEvent(
        record.order_link_id,
        record.exchange_order_id,
        record.parent_order_link_id,
        state,
        D(cumulative),
        at,
        {"state": state.value},
        record.symbol,
        record.qty,
        OrderType.MARKET,
        TimeInForce.IOC,
        None,
        Price(D(90)) if is_stop else None,
        record.purpose is not OrderPurpose.ENTRY,
        StopKind.PARTIAL_STOP_LOSS
        if record.parent_order_link_id
        else (StopKind.STOP_LOSS if is_stop else None),
    )


def execution(record, *, qty="0.4", exec_id="fill-1", at=NOW):
    return ExecutionEvent(
        exec_id,
        record.order_link_id,
        record.exchange_order_id,
        record.symbol,
        Qty(D(qty)),
        Price(D(100)),
        D("0.01"),
        at,
        False,
    )


def book(uow: UnitOfWork, fill: ExecutionEvent, attribution: Attribution) -> None:
    """Test-only quantity hook. Real accounting/external allocation belongs to M1-04."""
    assert attribution.strategy is not None
    old = uow.positions.get(attribution.strategy, fill.symbol)
    uow.positions.set(attribution.strategy, fill.symbol, Qty(old.value + fill.qty.value))


async def seed(db, *records):
    async with db.unit_of_work() as uow:
        for record in records:
            uow.orders.put(record)
        await uow.commit()


async def snapshot(db):
    async with db.unit_of_work() as uow:
        return uow.orders.all(), uow.fills.all(), uow.positions.all(), uow.order_events.all()


@pytest.mark.parametrize(
    "updates",
    [
        {"order_qty": Qty(D(0))},
        {"order_type": OrderType.LIMIT},
        {"limit_price": Price(D(100))},
        {"stop_kind": StopKind.STOP_LOSS, "reduce_only": True},
        {"stop_kind": StopKind.STOP_LOSS, "trigger_price": Price(D(90))},
        {
            "parent_order_link_id": "d-S1-E-parent",
            "stop_kind": StopKind.STOP_LOSS,
            "trigger_price": Price(D(90)),
            "reduce_only": True,
        },
        {"symbol": "BTCUSDT"},
        {"order_qty": D(1)},
        {"time_in_force": "IOC"},
        {"order_type": "MARKET"},
        {"reduce_only": 1},
        {"trigger_price": D(90)},
    ],
)
def test_extended_order_event_validation(updates):
    with pytest.raises((TypeError, ValueError)):
        replace(event(order()), **updates)


def test_valid_limit_and_attached_stop():
    limit = replace(
        event(order()),
        order_type=OrderType.LIMIT,
        limit_price=Price(D(100)),
        time_in_force=TimeInForce.GTC,
    )
    assert limit.limit_price.value == D(100)
    stop = replace(
        event(order()),
        order_qty=Qty(D(-1)),
        parent_order_link_id="d-S1-E-parent",
        trigger_price=Price(D(90)),
        reduce_only=True,
        stop_kind=StopKind.PARTIAL_STOP_LOSS,
    )
    assert stop.parent_order_link_id


@pytest.mark.parametrize("start", [OrderState.CREATED, OrderState.SUBMITTED])
def test_jump_to_filled_and_duplicate(start):
    record, transition = apply_order_event(
        order(state=start), event(order(), state=OrderState.FILLED, cumulative="1")
    )
    assert transition is Transition.ADVANCED
    assert record.state is OrderState.FILLED
    assert record.unbooked_qty == D(1)
    assert record.reserved_qty == 0
    second, transition = apply_order_event(
        record, event(record, state=OrderState.FILLED, cumulative="1")
    )
    assert transition is Transition.DUPLICATE
    assert second == record


def test_stale_state_still_preserves_new_cumulative_and_latest_timestamp():
    record = replace(
        order(state=OrderState.PARTIALLY_FILLED),
        updated_at=NOW + timedelta(seconds=2),
        exchange_cum_exec_qty=D("0.2"),
    )
    result, transition = apply_order_event(record, event(record, cumulative="0.4", at=NOW))
    assert transition is Transition.IGNORED_STALE
    assert result.state is OrderState.PARTIALLY_FILLED
    assert result.exchange_cum_exec_qty == D("0.4")
    assert result.updated_at == record.updated_at


@pytest.mark.parametrize(
    "terminal",
    [
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
        OrderState.ABORTED,
    ],
)
def test_terminal_state_is_fixed_but_cumulative_can_increase(terminal):
    record = order(state=terminal)
    updated, transition = apply_order_event(
        record, event(record, state=OrderState.PARTIALLY_FILLED, cumulative="0.4")
    )
    assert updated.state is terminal
    assert transition is Transition.TERMINAL_UNCHANGED
    assert updated.exchange_cum_exec_qty == D("0.4")


@pytest.mark.parametrize("state", [OrderState.CANCELLED, OrderState.FILLED])
@pytest.mark.parametrize("direction", [1, -1])
def test_terminal_notice_before_fill_keeps_exposure(state, direction):
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        record = order(qty=str(direction))
        await seed(db, record)
        await ingest_order_event(
            db.unit_of_work(),
            event(record, state=state, cumulative="0.4"),
            book,
            reference_price=Price(D(100)),
        )
        async with db.unit_of_work() as uow:
            view = exposure(uow, marks={BTC: Price(D(100))}, slippage_buffer=D(0))
            assert view.symbols[0].unbooked_qty == D("0.4") * direction
            assert view.total_notional == D(40)
            assert view.symbols[0].reserved_qty == 0
        assert (
            await apply_execution(
                db.unit_of_work(), execution(record, qty=str(D("0.4") * direction)), book
            )
            is FillResult.BOOKED
        )
        async with db.unit_of_work() as uow:
            view = exposure(uow, marks={BTC: Price(D(100))}, slippage_buffer=D(0))
            assert view.symbols[0].unbooked_qty == 0
            assert view.symbols[0].held_qty == D("0.4") * direction
            assert view.total_notional == D(40)
            assert uow.orders.get(record.key).state is state

    asyncio.run(scenario())


def test_execution_before_order_notice_and_duplicate_concurrency():
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        record = replace(order(state=OrderState.CREATED), exchange_order_id=None)
        await seed(db, record)
        fill = replace(execution(order(), qty="1"), exchange_order_id="exchange-one")
        outcomes = await asyncio.gather(
            *(apply_execution(db.unit_of_work(), fill, book) for _ in range(2))
        )
        assert set(outcomes) == {FillResult.BOOKED, FillResult.DUPLICATE}
        await ingest_order_event(
            db.unit_of_work(),
            event(order(), state=OrderState.FILLED, cumulative="1"),
            book,
            reference_price=Price(D(100)),
        )
        async with db.unit_of_work() as uow:
            assert len(uow.fills.all()) == 1
            assert uow.positions.get(StrategyId.S1, BTC).value == D(1)
            assert uow.orders.get(record.key).state is OrderState.FILLED

    asyncio.run(scenario())


def test_every_order_event_is_logged_including_stale_and_duplicate():
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        record = order()
        await seed(db, record)
        for state in (OrderState.FILLED, OrderState.FILLED, OrderState.OPEN):
            await ingest_order_event(
                db.unit_of_work(),
                event(record, state=state, cumulative="1"),
                book,
                reference_price=Price(D(100)),
            )
        async with db.unit_of_work() as uow:
            assert [e.transition for e in uow.order_events.all()] == [
                Transition.ADVANCED,
                Transition.DUPLICATE,
                Transition.TERMINAL_UNCHANGED,
            ]
            assert uow.order_events.all()[0].event.raw["state"] == "FILLED"

    asyncio.run(scenario())


def test_hook_failure_rolls_back_fill_order_and_position():
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        record = order()
        await seed(db, record)
        before = await snapshot(db)

        def broken(uow, fill, attribution):
            book(uow, fill, attribution)
            raise RuntimeError("book failed")

        with pytest.raises(RuntimeError):
            await apply_execution(db.unit_of_work(), execution(record), broken)
        assert await snapshot(db) == before
        assert (
            await apply_execution(db.unit_of_work(), execution(record), book) is FillResult.BOOKED
        )

    asyncio.run(scenario())


def test_commit_failure_and_task_cancellation_do_not_publish():
    class FailingCommit(InMemoryUnitOfWork):
        async def commit(self):
            raise OSError("database unavailable")

    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        record = order()
        await seed(db, record)
        before = await snapshot(db)
        with pytest.raises(OSError):
            await apply_execution(FailingCommit(db), execution(record), book)
        assert await snapshot(db) == before
        entered = asyncio.Event()

        async def interrupted():
            async with db.unit_of_work() as uow:
                uow.positions.set(StrategyId.S1, BTC, Qty(D(10)))
                entered.set()
                await asyncio.Future()

        task = asyncio.create_task(interrupted())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await snapshot(db) == before

    asyncio.run(scenario())


def test_unit_of_work_rollback_does_not_erase_concurrent_commit():
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        ready, release = asyncio.Event(), asyncio.Event()

        async def first():
            async with db.unit_of_work() as uow:
                uow.positions.set(StrategyId.S1, BTC, Qty(D(10)))
                ready.set()
                await release.wait()  # No commit: rollback.

        async def second():
            await ready.wait()
            async with db.unit_of_work() as uow:
                assert uow.positions.get(StrategyId.S1, BTC).value == 0
                uow.positions.set(StrategyId.S1, BTC, Qty(D(20)))
                await uow.commit()

        one, two = asyncio.create_task(first()), asyncio.create_task(second())
        await ready.wait()
        release.set()
        await asyncio.gather(one, two)
        async with db.unit_of_work() as uow:
            assert uow.positions.get(StrategyId.S1, BTC).value == D(20)

    asyncio.run(scenario())


def test_delayed_attached_stop_only_decrements_parent_strategy():
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        parent = order()
        stop = replace(
            order(purpose=OrderPurpose.ATTACHED_SL, qty="-0.4", suffix="stop"),
            parent_order_link_id=parent.order_link_id,
        )
        await seed(db, parent)
        async with db.unit_of_work() as uow:
            for strategy in StrategyId:
                uow.positions.set(strategy, BTC, Qty(D(1)))
            await uow.commit()
        fill = execution(stop, qty="-0.4")
        assert (
            await apply_execution(db.unit_of_work(), fill, book) is FillResult.PENDING_ATTRIBUTION
        )
        assert await apply_execution(db.unit_of_work(), fill, book) is FillResult.DUPLICATE
        async with db.unit_of_work() as uow:
            work = pending_work(
                uow,
                now=NOW + timedelta(seconds=20),
                query_after=timedelta(seconds=5),
                alert_after=timedelta(seconds=10),
            )
            assert work.paused_symbols == {BTC}
            assert work.query_order_ids == ("exchange-stop",)
            assert work.alert_order_ids == ("exchange-stop",)
            assert uow.positions.get(StrategyId.S1, BTC).value == D(1)
        result = await ingest_order_event(
            db.unit_of_work(),
            event(stop, state=OrderState.FILLED, cumulative="0.4"),
            book,
            reference_price=Price(D(100)),
        )
        assert result[1] == (FillResult.BOOKED,)
        async with db.unit_of_work() as uow:
            assert uow.positions.get(StrategyId.S1, BTC).value == D("0.6")
            assert uow.positions.get(StrategyId.S2, BTC).value == D(1)
            assert uow.fills.get("fill-1").status is FillStatus.BOOKED
        assert await apply_execution(db.unit_of_work(), fill, book) is FillResult.DUPLICATE

    asyncio.run(scenario())


def test_pending_resolution_hook_failure_rolls_back_event_order_and_fill():
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        record = order()
        await apply_execution(db.unit_of_work(), execution(record), book)
        before = await snapshot(db)

        def broken(uow, fill, attribution):
            book(uow, fill, attribution)
            raise RuntimeError("failed resolution")

        with pytest.raises(RuntimeError):
            await ingest_order_event(
                db.unit_of_work(), event(record), broken, reference_price=Price(D(100))
            )
        assert await snapshot(db) == before
        await ingest_order_event(
            db.unit_of_work(), event(record), book, reference_price=Price(D(100))
        )
        async with db.unit_of_work() as uow:
            assert uow.positions.get(StrategyId.S1, BTC).value == D("0.4")

    asyncio.run(scenario())


def test_external_requires_successful_lookup_not_a_foreign_ws_message():
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        external = replace(order(purpose=OrderPurpose.CLOSE, qty="-1"), order_link_id=None)
        calls = []

        def external_hook(uow, fill, attribution):
            assert attribution.external
            calls.append(fill.exec_id)

        await apply_execution(db.unit_of_work(), execution(external, qty="-0.4"), external_hook)
        await ingest_order_event(
            db.unit_of_work(), event(external), external_hook, reference_price=Price(D(100))
        )
        assert calls == []
        await ingest_order_event(
            db.unit_of_work(),
            event(external),
            external_hook,
            reference_price=Price(D(100)),
            lookup_confirmed=True,
        )
        assert calls == ["fill-1"]
        assert (
            await apply_execution(db.unit_of_work(), execution(external), external_hook)
            is FillResult.DUPLICATE
        )

    asyncio.run(scenario())


def test_other_environment_identity_stays_pending():
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        foreign = replace(order(), order_link_id="l-S1-E-one")
        await apply_execution(db.unit_of_work(), execution(foreign), book)
        await ingest_order_event(
            db.unit_of_work(),
            event(foreign),
            book,
            reference_price=Price(D(100)),
            lookup_confirmed=True,
        )
        async with db.unit_of_work() as uow:
            assert uow.fills.get("fill-1").status is FillStatus.PENDING_ATTRIBUTION
            assert not uow.orders.all()

    asyncio.run(scenario())


@pytest.mark.parametrize("direction", [1, -1])
def test_unfilled_stops_closes_and_unbooked_closes_never_reduce_exposure(direction):
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        close = replace(
            order(purpose=OrderPurpose.CLOSE, qty=str(-direction), suffix="close"),
            exchange_cum_exec_qty=D("0.4"),
            state=OrderState.CANCELLED,
        )
        stop = order(purpose=OrderPurpose.PROTECT, qty=str(-direction), suffix="stop")
        await seed(db, close, stop)
        async with db.unit_of_work() as uow:
            uow.positions.set(StrategyId.S1, BTC, Qty(D(direction)))
            view = exposure(uow, marks={BTC: Price(D(100))}, slippage_buffer=D("0.01"))
            assert view.total_notional == D(100)
            assert view.symbols[0].unbooked_qty == view.symbols[0].reserved_qty == 0

    asyncio.run(scenario())


def test_account_notional_does_not_net_contracts_or_opposite_pending_entries():
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        await seed(
            db,
            order(qty="1"),
            order(qty="-1", suffix="two"),
            order(qty="-1", suffix="eth", symbol=Symbol.ETHUSDT),
        )
        async with db.unit_of_work() as uow:
            view = exposure(uow, marks={}, slippage_buffer=D("0.1"))
            assert view.total_notional == D(330)
            assert view.symbols[0].reserved_qty == 0

    asyncio.run(scenario())


def test_unbooked_timeout_uses_first_seen_and_includes_close_fills():
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        record = replace(
            order(purpose=OrderPurpose.CLOSE, qty="-1"),
            exchange_cum_exec_qty=D("0.4"),
            updated_at=NOW + timedelta(seconds=29),
        )
        await seed(db, record)
        async with db.unit_of_work() as uow:
            result = unbooked_work(
                uow,
                first_seen={record.key: NOW},
                now=NOW + timedelta(seconds=30),
                timeout=timedelta(seconds=30),
            )
            assert result.order_keys == (record.key,)
            assert result.paused_symbols == {BTC}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "cancel",
    [CancelResult.ACCEPTED, CancelResult.NOT_FOUND, CancelResult.TOO_LATE, CancelResult.ERROR],
)
def test_unknown_empty_and_error_lookups_never_release_reservation(cancel):
    record = order(state=OrderState.UNKNOWN)
    for minute in range(1, 5):
        result = resolve_unknown(
            record,
            (LookupResult(True), LookupResult(False), LookupResult(True)),
            cancel,
            now=NOW + timedelta(minutes=minute),
            unknown_since=NOW,
            alert_after=timedelta(minutes=2),
            alerted=minute > 2,
            last_cancel_at=NOW,
            cancel_retry_after=timedelta(hours=1),
        )
        assert result.record.state is OrderState.UNKNOWN
        assert result.record.reserved_qty == D(1)
        assert result.action is (
            RecoveryAction.ALERT_MANUAL_CHECK if minute == 2 else RecoveryAction.KEEP_UNKNOWN
        )


@pytest.mark.parametrize("purpose", list(OrderPurpose))
def test_only_entry_unknown_requests_cancel(purpose):
    record = order(state=OrderState.UNKNOWN, purpose=purpose)
    result = resolve_unknown(
        record,
        (),
        CancelResult.NOT_ATTEMPTED,
        now=NOW,
        unknown_since=NOW,
        alert_after=timedelta(minutes=2),
        alerted=False,
        last_cancel_at=None,
        cancel_retry_after=timedelta(minutes=1),
    )
    assert result.action is (
        RecoveryAction.SEND_CANCEL if purpose is OrderPurpose.ENTRY else RecoveryAction.KEEP_UNKNOWN
    )


def test_unknown_moves_only_on_actual_exchange_status():
    record = order(state=OrderState.UNKNOWN, qty="-1")
    partial = event(record, state=OrderState.PARTIALLY_FILLED, cumulative="0.4")
    result = resolve_unknown(
        record,
        (LookupResult(True, partial),),
        CancelResult.ACCEPTED,
        now=NOW,
        unknown_since=NOW,
        alert_after=timedelta(minutes=2),
        alerted=False,
        last_cancel_at=NOW,
        cancel_retry_after=timedelta(minutes=1),
    )
    assert result.action is RecoveryAction.MOVE_TO
    assert result.record.reserved_qty == D("-0.6")
    assert result.record.unbooked_qty == D("-0.4")
    cancelled = event(record, state=OrderState.CANCELLED, cumulative="0.4")
    result = resolve_unknown(
        record,
        (LookupResult(True, cancelled),),
        CancelResult.ACCEPTED,
        now=NOW,
        unknown_since=NOW,
        alert_after=timedelta(minutes=2),
        alerted=False,
        last_cancel_at=NOW,
        cancel_retry_after=timedelta(minutes=1),
    )
    assert result.record.reserved_qty == 0
    assert result.record.unbooked_qty == D("-0.4")


def test_manual_absence_requires_unknown_and_actor_then_late_event_corrects():
    with pytest.raises(ValueError):
        confirm_not_found(order(), "operator", NOW)
    record = order(state=OrderState.UNKNOWN)
    with pytest.raises(ValueError):
        confirm_not_found(record, "", NOW)
    absent, audit = confirm_not_found(record, "operator", NOW)
    assert audit.actor == "operator"
    assert absent.reserved_qty == 0
    corrected, transition = apply_order_event(absent, event(record))
    assert transition is Transition.CORRECTED_FROM_NOT_FOUND
    assert corrected.state is OrderState.OPEN
    assert corrected.reserved_qty == D(1)


def test_late_execution_after_manual_not_found_is_booked_and_corrects_state():
    async def scenario():
        db = InMemoryDatabase("actual", Environment.DEMO)
        absent, _ = confirm_not_found(order(state=OrderState.UNKNOWN), "operator", NOW)
        await seed(db, absent)
        result = await apply_execution(db.unit_of_work(), execution(absent), book)
        assert result is FillResult.CORRECTED_FROM_NOT_FOUND
        async with db.unit_of_work() as uow:
            assert uow.orders.get(absent.key).state is OrderState.PARTIALLY_FILLED
            assert uow.orders.get(absent.key).booked_qty.value == D("0.4")

    asyncio.run(scenario())


def test_account_isolation_and_fill_repository_uniqueness():
    async def scenario():
        actual = InMemoryDatabase("actual", Environment.DEMO)
        shadow = InMemoryDatabase("shadow", Environment.BACKTEST)
        await seed(actual, order())
        await apply_execution(actual.unit_of_work(), execution(order()), book)
        async with shadow.unit_of_work() as uow:
            assert uow.positions.all() == ()
            assert uow.fills.all() == ()
        async with actual.unit_of_work() as uow:
            fill = uow.fills.get("fill-1")
            with pytest.raises(ValueError):
                uow.fills.add(fill)
        saved_uow = actual.unit_of_work()
        with pytest.raises(RuntimeError):
            saved_uow.orders.all()

    asyncio.run(scenario())


@pytest.mark.parametrize("direction", ["2", "-2"])
def test_pending_exposure_released_atomically_after_attribution(direction):
    async def scenario():
        db = InMemoryDatabase("pending", Environment.DEMO)
        record = order(qty=direction)
        fill = execution(record, qty=direction)
        assert (
            await apply_execution(db.unit_of_work(), fill, book) is FillResult.PENDING_ATTRIBUTION
        )
        assert await apply_execution(db.unit_of_work(), fill, book) is FillResult.DUPLICATE
        async with db.unit_of_work() as uow:
            view = exposure(uow, marks={}, slippage_buffer=D(0))
            assert view.total_notional == D(200)
            assert view.symbols[0].pending_qty == D(direction)
            assert view.symbols[0].pending_notional == D(200)
            assert (
                exposure(uow, marks={}, slippage_buffer=D(0), symbol=Symbol.ETHUSDT).total_notional
                == 0
            )
            assert exposure(
                uow, marks={}, slippage_buffer=D(0), strategy=StrategyId.S2
            ).total_notional == D(200)

        def failing_hook(uow, event, attribution):
            book(uow, event, attribution)
            raise RuntimeError("rollback attribution")

        confirmed = event(record, state=OrderState.FILLED, cumulative="2")
        with pytest.raises(RuntimeError):
            await ingest_order_event(
                db.unit_of_work(), confirmed, failing_hook, reference_price=Price(D(100))
            )
        async with db.unit_of_work() as uow:
            assert exposure(uow, marks={}, slippage_buffer=D(0)).total_notional == D(200)
        await ingest_order_event(db.unit_of_work(), confirmed, book, reference_price=Price(D(100)))
        async with db.unit_of_work() as uow:
            view = exposure(uow, marks={BTC: Price(D(100))}, slippage_buffer=D(0))
            assert view.total_notional == D(200)
            assert view.symbols[0].pending_notional == 0
            assert view.symbols[0].held_qty == D(direction)

    asyncio.run(scenario())


def test_opposite_pending_fills_add_notional_instead_of_netting():
    async def scenario():
        db = InMemoryDatabase("pending", Environment.DEMO)
        await apply_execution(db.unit_of_work(), execution(order(), qty="2"), book)
        await apply_execution(
            db.unit_of_work(), execution(order(suffix="other"), qty="-2", exec_id="other"), book
        )
        async with db.unit_of_work() as uow:
            view = exposure(uow, marks={}, slippage_buffer=D(0))
            assert view.symbols[0].pending_qty == 0
            assert view.total_notional == D(400)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "cancel", [CancelResult.NOT_FOUND, CancelResult.TOO_LATE, CancelResult.ERROR]
)
@pytest.mark.parametrize("purpose", list(OrderPurpose))
def test_unknown_cancel_retry_deadline_and_repeated_attempts(cancel, purpose):
    record = order(state=OrderState.UNKNOWN, purpose=purpose)

    def decide(at, last, result=cancel, results=()):
        return resolve_unknown(
            record,
            results,
            result,
            now=at,
            unknown_since=NOW,
            alert_after=timedelta(hours=1),
            alerted=False,
            last_cancel_at=last,
            cancel_retry_after=timedelta(seconds=30),
        )

    assert decide(NOW + timedelta(seconds=29), NOW).action is RecoveryAction.KEEP_UNKNOWN
    due = decide(NOW + timedelta(seconds=30), NOW)
    assert due.action is (
        RecoveryAction.SEND_CANCEL if purpose is OrderPurpose.ENTRY else RecoveryAction.KEEP_UNKNOWN
    )
    assert due.record.state is OrderState.UNKNOWN
    assert (
        decide(NOW + timedelta(seconds=59), NOW + timedelta(seconds=30)).action
        is RecoveryAction.KEEP_UNKNOWN
    )
    assert decide(NOW + timedelta(seconds=60), NOW + timedelta(seconds=30)).action is due.action
    assert (
        decide(NOW + timedelta(seconds=60), NOW, CancelResult.ACCEPTED).action
        is RecoveryAction.KEEP_UNKNOWN
    )
    resolved = decide(
        NOW + timedelta(seconds=60),
        NOW,
        CancelResult.ACCEPTED,
        (LookupResult(True, event(record, state=OrderState.CANCELLED)),),
    )
    assert resolved.action is RecoveryAction.MOVE_TO
    assert resolved.record.state is OrderState.CANCELLED


def test_unknown_alert_once_then_retry_does_not_release_reservation():
    record = order(state=OrderState.UNKNOWN)
    kwargs = dict(
        now=NOW + timedelta(minutes=10),
        unknown_since=NOW,
        alert_after=timedelta(minutes=10),
        last_cancel_at=NOW,
        cancel_retry_after=timedelta(seconds=30),
    )
    first = resolve_unknown(record, (), CancelResult.ERROR, alerted=False, **kwargs)
    assert first.action is RecoveryAction.ALERT_MANUAL_CHECK
    second = resolve_unknown(record, (), CancelResult.ERROR, alerted=first.alerted, **kwargs)
    assert second.action is RecoveryAction.SEND_CANCEL
    assert second.record.reserved_qty == D(1)


@pytest.mark.parametrize(
    "last,interval",
    [
        (None, timedelta(seconds=30)),
        (NOW + timedelta(seconds=1), timedelta(seconds=30)),
        (NOW.replace(tzinfo=None), timedelta(seconds=30)),
        (NOW, timedelta(0)),
        (NOW, timedelta(seconds=-1)),
    ],
)
def test_cancel_retry_rejects_invalid_timing(last, interval):
    with pytest.raises(ValueError):
        resolve_unknown(
            order(state=OrderState.UNKNOWN),
            (),
            CancelResult.ERROR,
            now=NOW,
            unknown_since=NOW,
            alert_after=timedelta(minutes=10),
            alerted=False,
            last_cancel_at=last,
            cancel_retry_after=interval,
        )
