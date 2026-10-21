"""Transactional fill ingestion and delayed attribution; no network calls."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from richletsgo.domain import (
    ExecutionEvent,
    OrderEvent,
    OrderPurpose,
    OrderState,
    Price,
    Qty,
    StrategyId,
    Symbol,
    parse_order_link_id,
)
from richletsgo.domain.clock import require_utc
from richletsgo.storage.contracts import FillRecord, FillStatus, LoggedOrderEvent, UnitOfWork

from .models import OrderRecord, Transition, apply_order_event


class FillResult(StrEnum):
    BOOKED = "BOOKED"
    DUPLICATE = "DUPLICATE"
    PENDING_ATTRIBUTION = "PENDING_ATTRIBUTION"
    EXTERNAL = "EXTERNAL"
    CORRECTED_FROM_NOT_FOUND = "CORRECTED_FROM_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class Attribution:
    strategy: StrategyId | None
    purpose: OrderPurpose | None
    external: bool


# Hooks must mutate only transactional repositories, never external objects/services.
type BookHook = Callable[[UnitOfWork, ExecutionEvent, Attribution], None]


def _book(uow: UnitOfWork, fill: FillRecord, order: OrderRecord, hook: BookHook) -> FillResult:
    event = fill.event
    if event.symbol is not order.symbol or (event.qty.value > 0) != (order.qty.value > 0):
        raise ValueError("execution symbol/direction does not match order")
    if order.exchange_order_id and order.exchange_order_id != event.exchange_order_id:
        raise ValueError("execution order identity mismatch")
    booked = order.booked_qty.value + event.qty.value
    corrected = order.state is OrderState.NOT_FOUND
    state = order.state
    if corrected:
        state = (
            OrderState.FILLED
            if booked.copy_abs() >= order.qty.value.copy_abs()
            else OrderState.PARTIALLY_FILLED
        )
    uow.orders.put(
        replace(
            order,
            booked_qty=Qty(booked),
            state=state,
            exchange_order_id=event.exchange_order_id,
            updated_at=max(order.updated_at, event.exec_time),
        )
    )
    uow.fills.replace(
        replace(fill, status=FillStatus.BOOKED, order_key=order.key, strategy=order.strategy)
    )
    hook(uow, event, Attribution(order.strategy, order.purpose, False))
    return FillResult.CORRECTED_FROM_NOT_FOUND if corrected else FillResult.BOOKED


async def apply_execution(uow: UnitOfWork, event: ExecutionEvent, hook: BookHook) -> FillResult:
    """Save/deduplicate a fill and invoke its book hook in the same transaction."""
    async with uow:
        if uow.fills.get(event.exec_id) is not None:
            return FillResult.DUPLICATE
        pending = FillRecord(event, FillStatus.PENDING_ATTRIBUTION, None, None)
        uow.fills.add(pending)
        order = uow.orders.find(event.order_link_id, event.exchange_order_id)
        result = FillResult.PENDING_ATTRIBUTION
        if order is not None:
            result = _book(uow, pending, order, hook)
        await uow.commit()
        return result


def _adopt(uow: UnitOfWork, event: OrderEvent, reference_price: Price) -> OrderRecord | None:
    """Recognize account/environment IDs, or resolve an attached stop through its parent."""
    if event.parent_order_link_id:
        parent = uow.orders.find(event.parent_order_link_id, None)
        if parent is None or parent.purpose is not OrderPurpose.ENTRY:
            return None
        if parent.symbol is not event.symbol:
            raise ValueError("stop parent symbol mismatch")
        if (parent.qty.value > 0) == (event.order_qty.value > 0):
            raise ValueError("stop must reduce its parent direction")
        strategy, purpose = parent.strategy, OrderPurpose.ATTACHED_SL
    else:
        parts = parse_order_link_id(event.order_link_id) if event.order_link_id else None
        if parts is None or parts.environment is not uow.environment:
            return None
        strategy, purpose = parts.strategy, parts.purpose
        if purpose in (OrderPurpose.CLOSE, OrderPurpose.PROTECT) and not event.reduce_only:
            raise ValueError("protective/close identity must be reduce-only")
        if purpose is OrderPurpose.ENTRY and event.reduce_only:
            raise ValueError("entry identity cannot be reduce-only")
    return OrderRecord(
        event.order_link_id,
        event.exchange_order_id,
        event.parent_order_link_id,
        strategy,
        purpose,
        event.symbol,
        event.order_qty,
        event.limit_price,
        reference_price,
        event.state,
        Qty(Decimal(0)),
        event.exchange_cum_exec_qty,
        event.event_time,
        event.event_time,
        trigger_price=event.trigger_price,
    )


async def ingest_order_event(
    uow: UnitOfWork,
    event: OrderEvent,
    hook: BookHook,
    *,
    reference_price: Price,
    lookup_confirmed: bool = False,
) -> tuple[Transition | None, tuple[FillResult, ...]]:
    """Log EVERY event and release pending fills atomically after identity is known.

    lookup_confirmed is set only by a successful REST order lookup, never a WS event.
    It authorizes EXTERNAL classification only for actual returned foreign orders.
    """
    async with uow:
        record = uow.orders.find(event.order_link_id, event.exchange_order_id)
        transition: Transition | None = None
        if record is not None:
            record, transition = apply_order_event(record, event)
        else:
            record = _adopt(uow, event, reference_price)
        if record is not None:
            uow.orders.put(record)
        uow.order_events.append(LoggedOrderEvent(event, transition))
        results: list[FillResult] = []
        foreign = all(
            parse_order_link_id(value) is None
            for value in (event.order_link_id, event.parent_order_link_id)
            if value is not None
        )
        for fill in uow.fills.all():
            if fill.status is not FillStatus.PENDING_ATTRIBUTION:
                continue
            if fill.event.exchange_order_id != event.exchange_order_id:
                continue
            if fill.event.symbol is not event.symbol:
                raise ValueError("pending execution symbol mismatch")
            if record is not None:
                result = _book(uow, fill, record, hook)
                results.append(result)
                record = uow.orders.get(record.key)
                assert record is not None
            elif lookup_confirmed and foreign:
                uow.fills.replace(replace(fill, status=FillStatus.EXTERNAL))
                hook(uow, fill.event, Attribution(None, None, True))
                results.append(FillResult.EXTERNAL)
        await uow.commit()
        return transition, tuple(results)


@dataclass(frozen=True, slots=True)
class PendingWork:
    paused_symbols: frozenset[Symbol]
    query_order_ids: tuple[str, ...]
    alert_order_ids: tuple[str, ...]


def pending_work(
    uow: UnitOfWork, *, now: datetime, query_after: timedelta, alert_after: timedelta
) -> PendingWork:
    """Describe contract pauses and lookup/alert work; caller performs external actions."""
    require_utc(now)
    if query_after < timedelta(0) or alert_after < query_after:
        raise ValueError("invalid pending deadlines")
    pending = [f.event for f in uow.fills.all() if f.status is FillStatus.PENDING_ATTRIBUTION]
    return PendingWork(
        frozenset(e.symbol for e in pending),
        tuple(sorted({e.exchange_order_id for e in pending if now - e.exec_time >= query_after})),
        tuple(sorted({e.exchange_order_id for e in pending if now - e.exec_time >= alert_after})),
    )
