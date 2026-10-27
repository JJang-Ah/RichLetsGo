"""Async exchange boundary. Signed quantities remain signed until an adapter sends them."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from richletsgo.domain import (
    ExecutionEvent,
    InstrumentSpec,
    OrderEvent,
    OrderPurpose,
    PositionSnapshot,
    Price,
    Qty,
    Symbol,
)
from richletsgo.domain.clock import require_utc
from richletsgo.oms.models import OrderRecord
from richletsgo.portfolio.models import StopParameters
from richletsgo.risk.equity import AccountSnapshot


@dataclass(frozen=True, slots=True)
class OrderRequest:
    order_link_id: str
    symbol: Symbol
    qty: Qty
    purpose: OrderPurpose
    reference_price: Price
    limit_price: Price | None = None
    stop: StopParameters | None = None
    trigger_price: Price | None = None
    expires_at: datetime | None = None
    post_only: bool = False

    @classmethod
    def from_record(
        cls, record: OrderRecord, *, expires_at: datetime | None = None
    ) -> "OrderRequest":
        if record.order_link_id is None:
            raise ValueError("new requests require an order link ID")
        return cls(
            record.order_link_id,
            record.symbol,
            record.qty,
            record.purpose,
            record.reference_price,
            record.limit_price,
            record.attached_stop,
            record.trigger_price,
            record.expires_at if expires_at is None else expires_at,
            record.post_only,
        )

    def __post_init__(self) -> None:
        if not self.order_link_id or not self.order_link_id.strip():
            raise ValueError("order link ID required")
        if not isinstance(self.symbol, Symbol) or not isinstance(self.purpose, OrderPurpose):
            raise TypeError("domain symbol and purpose required")
        if not isinstance(self.qty, Qty) or not self.qty.value:
            raise ValueError("nonzero signed Qty required")
        if not isinstance(self.reference_price, Price):
            raise TypeError("reference price required")
        for price in (self.limit_price, self.trigger_price):
            if price is not None and not isinstance(price, Price):
                raise TypeError("optional prices must be Price")
        if self.stop is not None and not isinstance(self.stop, StopParameters):
            raise TypeError("stop must be StopParameters")
        if type(self.post_only) is not bool or (self.post_only and self.limit_price is None):
            raise ValueError("post_only requires a limit order")
        if self.expires_at is not None:
            require_utc(self.expires_at)

    @property
    def reduce_only(self) -> bool:
        return self.purpose is not OrderPurpose.ENTRY


class ErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    TOO_LATE = "TOO_LATE"
    TIMEOUT = "TIMEOUT"
    REJECTED = "REJECTED"
    QUERY_FAILED = "QUERY_FAILED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class ExchangeError:
    code: ErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class Accepted:
    order_link_id: str | None
    exchange_order_id: str


type CommandResult = Accepted | ExchangeError
type PrivateEvent = OrderEvent | ExecutionEvent | PositionSnapshot


class ExchangeGateway(Protocol):
    @property
    def connected(self) -> bool: ...
    async def place_order(self, request: OrderRequest) -> CommandResult: ...
    async def cancel_order(self, order_link_id: str) -> CommandResult: ...
    async def amend_stop(
        self, order_link_id: str, trigger_price: Price | None = None, qty: Qty | None = None
    ) -> CommandResult: ...
    async def get_open_orders(self, symbol: Symbol) -> tuple[OrderEvent, ...] | ExchangeError: ...
    async def get_order_history(
        self, symbol: Symbol, since: datetime
    ) -> tuple[OrderEvent, ...] | ExchangeError: ...
    async def get_executions(
        self, symbol: Symbol, since: datetime
    ) -> tuple[ExecutionEvent, ...] | ExchangeError: ...
    async def get_positions(self) -> tuple[PositionSnapshot, ...] | ExchangeError: ...
    async def get_account(self) -> AccountSnapshot | ExchangeError: ...
    async def get_instrument(self, symbol: Symbol) -> InstrumentSpec | ExchangeError: ...
    def private_events(self) -> AsyncIterator[PrivateEvent]: ...
