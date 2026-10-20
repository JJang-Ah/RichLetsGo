"""Transactional repositories; no external database implementation."""

from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self

from richletsgo.domain import Environment, ExecutionEvent, OrderEvent, Qty, StrategyId, Symbol
from richletsgo.oms.models import OrderRecord, Transition


class FillStatus(StrEnum):
    BOOKED = "BOOKED"
    PENDING_ATTRIBUTION = "PENDING_ATTRIBUTION"
    EXTERNAL = "EXTERNAL"


@dataclass(frozen=True, slots=True)
class FillRecord:
    event: ExecutionEvent
    status: FillStatus
    order_key: str | None
    strategy: StrategyId | None


@dataclass(frozen=True, slots=True)
class LoggedOrderEvent:
    event: OrderEvent
    transition: Transition | None


class OrderRepository(Protocol):
    def get(self, key: str) -> OrderRecord | None: ...
    def find(
        self, order_link_id: str | None, exchange_order_id: str | None
    ) -> OrderRecord | None: ...
    def put(self, order: OrderRecord) -> None: ...
    def all(self) -> tuple[OrderRecord, ...]: ...


class FillRepository(Protocol):
    def get(self, exec_id: str) -> FillRecord | None: ...
    def add(self, fill: FillRecord) -> None: ...
    def replace(self, fill: FillRecord) -> None: ...
    def all(self) -> tuple[FillRecord, ...]: ...


class OrderEventLog(Protocol):
    def append(self, event: LoggedOrderEvent) -> None: ...
    def all(self) -> tuple[LoggedOrderEvent, ...]: ...


class PositionRepository(Protocol):
    """Quantity-only hook storage; strategy accounting is implemented in M1-04."""

    def get(self, strategy: StrategyId, symbol: Symbol) -> Qty: ...
    def set(self, strategy: StrategyId, symbol: Symbol, qty: Qty) -> None: ...
    def all(self) -> tuple[tuple[StrategyId, Symbol, Qty], ...]: ...


class UnitOfWork(Protocol):
    environment: Environment
    orders: OrderRepository
    fills: FillRepository
    order_events: OrderEventLog
    positions: PositionRepository

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
