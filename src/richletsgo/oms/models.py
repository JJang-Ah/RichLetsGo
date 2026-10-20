"""Immutable OMS records and pure order-state transitions."""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from richletsgo.domain import OrderEvent, OrderPurpose, OrderState, Price, Qty, StrategyId, Symbol
from richletsgo.domain.clock import require_utc
from richletsgo.domain.values import finite_decimal

ZERO = Decimal(0)


class Transition(StrEnum):
    ADVANCED = "ADVANCED"
    DUPLICATE = "DUPLICATE"
    IGNORED_STALE = "IGNORED_STALE"
    TERMINAL_UNCHANGED = "TERMINAL_UNCHANGED"
    CORRECTED_FROM_NOT_FOUND = "CORRECTED_FROM_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class OrderRecord:
    order_link_id: str | None
    exchange_order_id: str | None
    parent_order_link_id: str | None
    strategy: StrategyId
    purpose: OrderPurpose
    symbol: Symbol
    qty: Qty
    limit_price: Price | None
    reference_price: Price
    state: OrderState
    booked_qty: Qty
    exchange_cum_exec_qty: Decimal
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.order_link_id is None and self.exchange_order_id is None:
            raise ValueError("order needs at least one identity")
        for value in (self.order_link_id, self.exchange_order_id, self.parent_order_link_id):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("order identities must be nonempty strings")
        if not isinstance(self.strategy, StrategyId) or not isinstance(self.purpose, OrderPurpose):
            raise TypeError("strategy and purpose must be enums")
        if not isinstance(self.symbol, Symbol) or not isinstance(self.state, OrderState):
            raise TypeError("symbol and state must be enums")
        if not isinstance(self.qty, Qty) or not isinstance(self.booked_qty, Qty):
            raise TypeError("order quantities must be Qty")
        if self.qty.value == 0:
            raise ValueError("order quantity must be nonzero")
        if self.booked_qty.value != 0 and (self.qty.value > 0) != (self.booked_qty.value > 0):
            raise ValueError("booked quantity has the wrong direction")
        if not isinstance(self.reference_price, Price):
            raise TypeError("reference_price must be Price")
        if self.limit_price is not None and not isinstance(self.limit_price, Price):
            raise TypeError("limit_price must be Price or None")
        if finite_decimal(self.exchange_cum_exec_qty, "cumulative quantity") < 0:
            raise ValueError("cumulative quantity must be nonnegative")
        require_utc(self.created_at)
        require_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")

    @property
    def key(self) -> str:
        return (
            f"link:{self.order_link_id}"
            if self.order_link_id
            else f"exchange:{self.exchange_order_id}"
        )

    def _signed(self, value: Decimal) -> Decimal:
        return value if self.qty.value > 0 else value.copy_negate()

    @property
    def unbooked_qty(self) -> Decimal:
        if self.purpose is not OrderPurpose.ENTRY:
            return ZERO
        return self._signed(
            max(ZERO, self.exchange_cum_exec_qty - self.booked_qty.value.copy_abs())
        )

    @property
    def reserved_qty(self) -> Decimal:
        if self.purpose is not OrderPurpose.ENTRY or self.state.is_terminal:
            return ZERO
        return self._signed(
            max(
                ZERO,
                self.qty.value.copy_abs()
                - max(self.exchange_cum_exec_qty, self.booked_qty.value.copy_abs()),
            )
        )

    def conservative_price(self, slippage_buffer: Decimal) -> Decimal:
        if finite_decimal(slippage_buffer, "slippage_buffer") < 0:
            raise ValueError("slippage_buffer must be nonnegative")
        # Notional upper bound is the higher price for BOTH long and short.
        return (
            self.limit_price.value
            if self.limit_price is not None
            else self.reference_price.value * (1 + slippage_buffer)
        )


def apply_order_event(record: OrderRecord, event: OrderEvent) -> tuple[OrderRecord, Transition]:
    """Preserve terminal state and maximum cumulative fill even for stale events."""
    if record.symbol is not event.symbol or (record.qty.value > 0) != (event.order_qty.value > 0):
        raise ValueError("event symbol/direction does not match order")
    if record.exchange_order_id is not None and record.exchange_order_id != event.exchange_order_id:
        raise ValueError("exchange order ID mismatch")
    if record.order_link_id and event.order_link_id and record.order_link_id != event.order_link_id:
        raise ValueError("order link ID mismatch")
    state = record.state
    if state is event.state:
        transition = Transition.DUPLICATE
    elif state is OrderState.NOT_FOUND:
        state, transition = event.state, Transition.CORRECTED_FROM_NOT_FOUND
    elif state.is_terminal:
        transition = Transition.TERMINAL_UNCHANGED
    elif event.state.rank < state.rank:
        transition = Transition.IGNORED_STALE
    else:
        state, transition = event.state, Transition.ADVANCED
    fresh = event.event_time >= record.updated_at
    return replace(
        record,
        exchange_order_id=event.exchange_order_id,
        state=state,
        qty=event.order_qty if fresh else record.qty,
        limit_price=event.limit_price if fresh else record.limit_price,
        parent_order_link_id=event.parent_order_link_id or record.parent_order_link_id,
        exchange_cum_exec_qty=max(record.exchange_cum_exec_qty, event.exchange_cum_exec_qty),
        updated_at=max(record.updated_at, event.event_time),
    ), transition


@dataclass(frozen=True, slots=True)
class ManualResolution:
    order_key: str
    actor: str
    at: datetime


def confirm_not_found(
    record: OrderRecord, actor: str, at: datetime
) -> tuple[OrderRecord, ManualResolution]:
    """Only an explicitly attributed manual resolution may assert absence."""
    require_utc(at)
    if record.state is not OrderState.UNKNOWN:
        raise ValueError("only UNKNOWN may be manually confirmed absent")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor is required")
    return replace(
        record, state=OrderState.NOT_FOUND, updated_at=max(at, record.updated_at)
    ), ManualResolution(record.key, actor, at)
