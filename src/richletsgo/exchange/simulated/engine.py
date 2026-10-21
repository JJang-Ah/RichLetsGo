"""Deterministic OHLC exchange: one-way isolated positions, no liquidation model.

Call advance_bar for completed, nonoverlapping bars. Funding must be supplied
in that call at bar.end; it settles after fills, before any next-bar orders.
UTC start_day remains the caller's responsibility. Intrabar execution times
are synthetic (bar.end minus one microsecond); market orders use bar.start.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from richletsgo.domain import (
    ExecutionEvent,
    InstrumentSpec,
    OrderEvent,
    OrderPurpose,
    OrderState,
    OrderType,
    PositionSnapshot,
    Price,
    Qty,
    StopKind,
    Symbol,
    TimeInForce,
)
from richletsgo.domain.clock import Clock, require_utc
from richletsgo.domain.values import finite_decimal
from richletsgo.exchange.contracts import (
    Accepted,
    CommandResult,
    ErrorCode,
    ExchangeError,
    ExchangeGateway,
    OrderRequest,
    PrivateEvent,
)
from richletsgo.marketdata.models import Bar, FundingRate
from richletsgo.risk.equity import AccountSnapshot

from .faults import FaultKind, FaultPlan

ZERO = Decimal(0)


class StopReduction(StrEnum):
    NEWEST = "newest"
    OLDEST = "oldest"
    PREFERRED_PARENT = "preferred_parent"


@dataclass(frozen=True, slots=True)
class FundingPayment:
    record: FundingRate
    qty: Qty
    price: Price
    amount: Decimal


@dataclass(slots=True)
class _Order:
    request: OrderRequest
    exchange_id: str
    created_at: datetime
    updated_at: datetime
    parent: str | None = None
    state: OrderState = OrderState.OPEN
    cumulative: Decimal = ZERO
    hidden: int = 0
    hold_attached: bool = False

    @property
    def remaining(self) -> Decimal:
        return abs(self.request.qty.value) - self.cumulative

    def event(self) -> OrderEvent:
        r = self.request
        return OrderEvent(
            None if self.parent else r.order_link_id,
            self.exchange_id,
            self.parent,
            self.state,
            self.cumulative,
            self.updated_at,
            {},
            r.symbol,
            r.qty,
            OrderType.LIMIT if r.limit_price else OrderType.MARKET,
            TimeInForce.GTC if r.limit_price or r.trigger_price else TimeInForce.IOC,
            r.limit_price,
            r.trigger_price,
            r.reduce_only,
            (StopKind.PARTIAL_STOP_LOSS if self.parent else StopKind.STOP_LOSS)
            if r.trigger_price
            else None,
        )


class SimulatedExchange:
    """Push-based data provider boundary; all inputs and faults are injected.

    NEWEST is the default stop reduction assumption, not a claim about Bybit.
    Attached stops have no client link ID; cancel/amend accept their exchange ID.
    One private stream consumer is supported; disconnect terminates its iterator.
    """

    def __init__(
        self,
        *,
        instruments: Sequence[InstrumentSpec],
        clock: Clock,
        wallet_balance: Decimal,
        maker_fee: Decimal,
        taker_fee: Decimal,
        slippage: Callable[[Price, Qty], Price],
        faults: FaultPlan | None = None,
        stop_reduction: StopReduction = StopReduction.NEWEST,
        preferred_parent: str | None = None,
    ) -> None:
        self._specs = {s.symbol: s for s in instruments}
        if not self._specs or len(self._specs) != len(instruments):
            raise ValueError("unique instruments required")
        self.clock = clock
        self.wallet = finite_decimal(wallet_balance, "wallet")
        self.maker_fee = finite_decimal(maker_fee, "maker fee")
        self.taker_fee = finite_decimal(taker_fee, "taker fee")
        self.slippage = slippage
        self.faults = faults or FaultPlan()
        self.reduction = stop_reduction
        self.preferred_parent = preferred_parent
        if stop_reduction is StopReduction.PREFERRED_PARENT and not preferred_parent:
            raise ValueError("preferred parent required")
        self._counts = [0] * len(self.faults.faults)
        self._orders: dict[str, _Order] = {}
        self._links: dict[str, str] = {}
        self._executions: list[ExecutionEvent] = []
        self._positions: dict[Symbol, tuple[Decimal, Decimal]] = {
            symbol: (ZERO, ZERO) for symbol in self._specs
        }
        self._marks: dict[Symbol, Decimal] = {}
        self._ends: dict[Symbol, datetime] = {}
        self._payments: list[FundingPayment] = []
        self._held: dict[str, OrderEvent] = {}
        self._queue: asyncio.Queue[PrivateEvent | None] = asyncio.Queue()
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def funding_payments(self) -> tuple[FundingPayment, ...]:
        return tuple(self._payments)

    def disconnect(self) -> None:
        if self._connected:
            self._connected = False
            self._queue.put_nowait(None)

    def reconnect(self) -> None:
        if not self._connected:
            self._queue = asyncio.Queue()
            self._connected = True

    async def private_events(self) -> AsyncIterator[PrivateEvent]:
        queue = self._queue
        while True:
            event = await queue.get()
            if event is None:
                return
            yield event

    def drain_events(self) -> tuple[PrivateEvent, ...]:
        events: list[PrivateEvent] = []
        while not self._queue.empty():
            event = self._queue.get_nowait()
            if event is not None:
                events.append(event)
        return tuple(events)

    def _emit(self, event: PrivateEvent) -> None:
        if self.connected:
            self._queue.put_nowait(event)

    def _fault(self, operation: str, link: str | None = None) -> dict[FaultKind, Decimal]:
        result: dict[FaultKind, Decimal] = {}
        for i, fault in enumerate(self.faults.faults):
            if fault.operation == operation and fault.order_link_id in (None, link):
                self._counts[i] += 1
                if self._counts[i] == fault.occurrence:
                    result[fault.kind] = fault.value
        if FaultKind.DISCONNECT in result:
            self.disconnect()
        return result

    def _change(self, order: _Order, state: OrderState, at: datetime) -> None:
        order.state, order.updated_at = state, at
        self._emit(order.event())

    def _lookup(self, identifier: str) -> _Order | None:
        return self._orders.get(self._links.get(identifier, identifier))

    async def place_order(self, request: OrderRequest) -> CommandResult:
        at = require_utc(self.clock.now())
        faults = self._fault("place", request.order_link_id)
        reason = self._validate(request, at)
        if reason or FaultKind.REJECT in faults:
            return ExchangeError(ErrorCode.REJECTED, reason or "injected rejection")
        spec = self._specs[request.symbol]
        qty = spec.round_qty_down(abs(request.qty.value))
        request = replace(request, qty=Qty(qty if request.qty.value > 0 else -qty))
        oid = f"sim-order-{len(self._orders) + 1}"
        order = _Order(
            request,
            oid,
            at,
            at,
            hidden=int(faults.get(FaultKind.HIDE_ORDER, ZERO)),
            hold_attached=FaultKind.HOLD_ATTACHED in faults,
        )
        self._orders[oid] = order
        self._links[request.order_link_id] = oid
        self._emit(order.event())
        if request.trigger_price:
            self._shrink_stops(request.symbol, at)
        if FaultKind.RESPONSE_LOST in faults:
            return ExchangeError(ErrorCode.TIMEOUT, "accepted; response lost")
        return Accepted(request.order_link_id, oid)

    def _validate(self, r: OrderRequest, at: datetime) -> str | None:
        if r.order_link_id.startswith("sim-order-"):
            return "reserved exchange ID prefix"
        if r.order_link_id in self._links or r.order_link_id in self._orders:
            return "duplicate order ID"
        if r.symbol not in self._specs:
            return "unknown instrument"
        if not self._specs[r.symbol].round_qty_down(abs(r.qty.value)):
            return "below minimum quantity"
        if r.expires_at is not None and r.expires_at <= at:
            return "expired request"
        if r.limit_price and r.trigger_price:
            return "stop limit unsupported"
        if r.purpose is OrderPurpose.ATTACHED_SL:
            return "attached stops are exchange generated"
        if r.purpose is OrderPurpose.ENTRY:
            if r.stop is None or r.trigger_price:
                return "entry requires attached stop"
            entry = r.limit_price or r.reference_price
            if (entry.value - r.stop.stop_loss.value) * r.qty.value <= 0:
                return "invalid stop direction"
            holding, _ = self._positions[r.symbol]
            if holding * r.qty.value < 0:
                return "opposite entry in one-way position"
        elif r.stop is not None:
            return "only entries attach stops"
        if r.purpose is OrderPurpose.PROTECT and r.trigger_price is None:
            return "protection requires trigger"
        if r.trigger_price is not None and r.purpose is not OrderPurpose.PROTECT:
            return "only protection requests may specify a trigger"
        return None

    async def cancel_order(self, order_link_id: str) -> CommandResult:
        faults = self._fault("cancel", order_link_id)
        order = self._lookup(order_link_id)
        if order is None or FaultKind.CANCEL_NOT_FOUND in faults:
            return ExchangeError(ErrorCode.NOT_FOUND, "order not found")
        if order.state.is_terminal or FaultKind.CANCEL_TOO_LATE in faults:
            return ExchangeError(ErrorCode.TOO_LATE, "order no longer cancellable")
        if FaultKind.CANCEL_RACE in faults:
            price = self._marks.get(order.request.symbol)
            if price is None:
                return ExchangeError(ErrorCode.REJECTED, "cancel race requires a market mark")
            self._fill(order, Price(price), self.clock.now(), maker=False)
            self._emit(order.event())
            return ExchangeError(ErrorCode.TOO_LATE, "fill won cancellation race")
        self._change(order, OrderState.CANCELLED, self.clock.now())
        if FaultKind.RESPONSE_LOST in faults:
            return ExchangeError(ErrorCode.TIMEOUT, "cancelled; response lost")
        return Accepted(order.event().order_link_id, order.exchange_id)

    async def amend_stop(self, order_link_id: str, trigger_price: Price) -> CommandResult:
        faults = self._fault("amend", order_link_id)
        order = self._lookup(order_link_id)
        if order is None:
            return ExchangeError(ErrorCode.NOT_FOUND, "order not found")
        if order.state.is_terminal:
            return ExchangeError(ErrorCode.TOO_LATE, "terminal stop")
        if order.request.trigger_price is None:
            return ExchangeError(ErrorCode.REJECTED, "not a stop")
        order.request = replace(order.request, trigger_price=trigger_price)
        self._change(order, order.state, self.clock.now())
        if FaultKind.RESPONSE_LOST in faults:
            return ExchangeError(ErrorCode.TIMEOUT, "amended; response lost")
        return Accepted(order.event().order_link_id, order.exchange_id)

    def _query_error(self, operation: str) -> ExchangeError | None:
        if FaultKind.QUERY_ERROR in self._fault(operation):
            return ExchangeError(ErrorCode.QUERY_FAILED, "injected query failure")
        return None

    def _visible(self, symbol: Symbol) -> tuple[_Order, ...]:
        result = []
        for order in self._orders.values():
            if order.request.symbol is symbol:
                if order.hidden:
                    order.hidden -= 1
                else:
                    result.append(order)
        return tuple(result)

    async def get_open_orders(self, symbol: Symbol) -> tuple[OrderEvent, ...] | ExchangeError:
        error = self._query_error("open_orders")
        if error:
            return error
        return tuple(o.event() for o in self._visible(symbol) if not o.state.is_terminal)

    async def get_order_history(
        self, symbol: Symbol, since: datetime
    ) -> tuple[OrderEvent, ...] | ExchangeError:
        require_utc(since)
        error = self._query_error("history")
        if error:
            return error
        return tuple(o.event() for o in self._visible(symbol) if o.updated_at >= since)

    async def get_executions(
        self, symbol: Symbol, since: datetime
    ) -> tuple[ExecutionEvent, ...] | ExchangeError:
        require_utc(since)
        error = self._query_error("executions")
        return error or tuple(
            e for e in self._executions if e.symbol is symbol and e.exec_time >= since
        )

    def _position(self, symbol: Symbol, at: datetime) -> PositionSnapshot:
        qty, avg = self._positions[symbol]
        return PositionSnapshot(symbol, Qty(qty), Price(avg) if qty else None, at)

    async def get_positions(self) -> tuple[PositionSnapshot, ...] | ExchangeError:
        error = self._query_error("positions")
        return error or tuple(self._position(s, self.clock.now()) for s in self._specs)

    async def get_account(self) -> AccountSnapshot | ExchangeError:
        error = self._query_error("account")
        unrealized = sum(
            (qty * (self._marks.get(s, avg) - avg) for s, (qty, avg) in self._positions.items()),
            ZERO,
        )
        return error or AccountSnapshot(self.clock.now(), self.wallet, unrealized)

    async def get_instrument(self, symbol: Symbol) -> InstrumentSpec | ExchangeError:
        return (
            self._query_error("instrument")
            or self._specs.get(symbol)
            or ExchangeError(ErrorCode.NOT_FOUND, "instrument not found")
        )

    def _price(self, price: Price, qty: Qty) -> Price:
        slipped = self.slippage(price, qty)
        if (slipped.value - price.value) * qty.value < 0:
            raise ValueError("slippage must be adverse")
        return slipped

    def _fill(self, order: _Order, price: Price, at: datetime, *, maker: bool) -> _Order | None:
        r = order.request
        faults = self._fault("fill", r.order_link_id)
        holding, avg = self._positions[r.symbol]
        if FaultKind.REJECT in faults or (not r.reduce_only and holding * r.qty.value < 0):
            self._change(order, OrderState.REJECTED, at)
            return None
        amount = order.remaining
        if r.reduce_only:
            amount = min(amount, abs(holding)) if holding * r.qty.value < 0 else ZERO
        if FaultKind.PARTIAL_FILL in faults:
            amount = self._specs[r.symbol].round_qty_down(amount * faults[FaultKind.PARTIAL_FILL])
        if not amount:
            self._change(order, OrderState.CANCELLED, at)
            return None
        signed = amount if r.qty.value > 0 else -amount
        actual = price if maker else self._price(price, Qty(signed))
        fee = amount * actual.value * (self.maker_fee if maker else self.taker_fee)
        if r.reduce_only:
            self.wallet += (actual.value - avg) * (-signed) - fee
            new_avg = avg if holding + signed else ZERO
        else:
            new_avg = (abs(holding) * avg + amount * actual.value) / (abs(holding) + amount)
            self.wallet -= fee
        self._positions[r.symbol] = holding + signed, new_avg
        order.cumulative += amount
        order.updated_at = at
        order.state = (
            OrderState.FILLED
            if order.remaining == 0
            else OrderState.PARTIALLY_FILLED
            if r.limit_price
            else OrderState.CANCELLED
        )
        event = ExecutionEvent(
            f"sim-exec-{len(self._executions) + 1}",
            None if order.parent else r.order_link_id,
            order.exchange_id,
            r.symbol,
            Qty(signed),
            actual,
            fee,
            at,
            maker,
        )
        self._executions.append(event)
        child = None
        if not r.reduce_only:
            assert r.stop is not None
            oid = f"sim-order-{len(self._orders) + 1}"
            child = _Order(
                OrderRequest(
                    oid,
                    r.symbol,
                    Qty(-signed),
                    OrderPurpose.ATTACHED_SL,
                    actual,
                    trigger_price=r.stop.stop_loss,
                ),
                oid,
                at,
                at,
                parent=r.order_link_id,
            )
            self._orders[oid] = child
            if order.hold_attached:
                self._held[oid] = child.event()
            else:
                self._emit(child.event())
        events: tuple[PrivateEvent, ...] = (event, order.event())
        if FaultKind.ORDER_FIRST in faults:
            events = tuple(reversed(events))
        if order.exchange_id in self._held:
            events = (event, self._held.pop(order.exchange_id), order.event())
        for item in events:
            self._emit(item)
            if FaultKind.DUPLICATE in faults:
                self._emit(item)
        self._emit(self._position(r.symbol, at))
        self._shrink_stops(r.symbol, at)
        return child

    def _shrink_stops(self, symbol: Symbol, at: datetime) -> None:
        stops = [
            o
            for o in self._orders.values()
            if o.request.symbol is symbol and o.request.trigger_price and not o.state.is_terminal
        ]
        if self.reduction is StopReduction.NEWEST:
            stops.reverse()
        elif self.reduction is StopReduction.PREFERRED_PARENT:
            stops.sort(key=lambda o: o.parent != self.preferred_parent)
        excess = sum((o.remaining for o in stops), ZERO) - abs(self._positions[symbol][0])
        for order in stops:
            if excess <= 0:
                break
            cut = min(excess, order.remaining)
            excess -= cut
            if cut == order.remaining:
                self._change(order, OrderState.CANCELLED, at)
            else:
                sign = 1 if order.request.qty.value > 0 else -1
                order.request = replace(
                    order.request, qty=Qty((abs(order.request.qty.value) - cut) * sign)
                )
                self._change(order, order.state, at)

    def _trigger(self, order: _Order, bar: Bar) -> None:
        trigger = order.request.trigger_price
        assert trigger is not None
        sell = order.request.qty.value < 0
        if (sell and bar.low <= trigger.value) or (not sell and bar.high >= trigger.value):
            price = min(bar.open, trigger.value) if sell else max(bar.open, trigger.value)
            self._fill(order, Price(price), bar.end - timedelta(microseconds=1), maker=False)

    def advance_bar(self, bar: Bar, *, funding: Sequence[FundingRate] = ()) -> None:
        """Process one completed bar and its boundary funding as a single step.

        No order placed after bar.start is eligible. Supply smaller bars if a
        funding timestamp falls inside a bar; never silently move a settlement.
        """
        if bar.symbol not in self._specs or bar.end > self.clock.now():
            raise ValueError("known instrument and completed bar required")
        previous = self._ends.get(bar.symbol)
        if previous is not None and bar.start < previous:
            raise ValueError("bars must not overlap or repeat")
        if len(funding) > 1 or any(
            f.symbol is not bar.symbol or f.funding_time != bar.end for f in funding
        ):
            raise ValueError("funding must match this bar boundary exactly")
        eligible = [
            o
            for o in self._orders.values()
            if o.request.symbol is bar.symbol
            and not o.state.is_terminal
            and o.created_at <= bar.start
        ]
        if any(
            o.request.expires_at is not None and bar.start < o.request.expires_at < bar.end
            for o in eligible
        ):
            raise ValueError("use smaller bars at an intrabar expiry boundary")
        # OHLC cannot establish intrabar chronology: pessimistically prioritize stops.
        for order in eligible:
            if order.request.trigger_price and not order.state.is_terminal:
                self._trigger(order, bar)
        for order in eligible:
            r = order.request
            if order.state.is_terminal or r.trigger_price:
                continue
            if r.expires_at is not None and r.expires_at <= bar.start:
                self._change(order, OrderState.EXPIRED, bar.start)
                continue
            child = None
            if r.limit_price:
                crossed = (
                    bar.low < r.limit_price.value
                    if r.qty.value > 0
                    else bar.high > r.limit_price.value
                )
                if crossed:
                    child = self._fill(
                        order, r.limit_price, bar.end - timedelta(microseconds=1), maker=True
                    )
                if not order.state.is_terminal and (
                    r.expires_at is None or r.expires_at <= bar.end
                ):
                    self._change(order, OrderState.EXPIRED, bar.end)
            else:
                child = self._fill(order, Price(bar.open), bar.start, maker=False)
            if child is not None and not child.state.is_terminal:
                self._trigger(child, bar)
        self._marks[bar.symbol] = bar.close
        self._ends[bar.symbol] = bar.end
        for record in funding:
            qty, _ = self._positions[bar.symbol]
            amount = -qty * bar.close * record.rate
            self.wallet += amount
            self._payments.append(FundingPayment(record, Qty(qty), Price(bar.close), amount))


def as_gateway(exchange: SimulatedExchange) -> ExchangeGateway:
    """Compile-time protocol conformance check under strict mypy."""
    return exchange
