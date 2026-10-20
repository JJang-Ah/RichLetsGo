"""Immutable domain events, including read-only copies of order payloads."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from .clock import require_utc
from .enums import OrderState, OrderType, StopKind, Symbol, TimeInForce
from .values import Price, Qty, finite_decimal

type RawValue = str | int | float | bool | None | Mapping[str, RawValue] | tuple[RawValue, ...]


def _freeze(value: object) -> RawValue:
    # Raw exchange payloads are diagnostic data, not trading numeric inputs.
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, RawValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("raw payload keys must be strings")
            result[key] = _freeze(child)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    raise TypeError("raw payload must contain JSON-compatible values")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if not value or not value.strip():
        raise ValueError(f"{name} must be nonempty")


def _optional_id(value: str | None, name: str) -> None:
    if value is not None:
        _identifier(value, name)


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    exec_id: str
    order_link_id: str | None
    exchange_order_id: str
    symbol: Symbol
    qty: Qty
    price: Price
    fee: Decimal
    exec_time: datetime
    is_maker: bool

    def __post_init__(self) -> None:
        _identifier(self.exec_id, "exec_id")
        _identifier(self.exchange_order_id, "exchange_order_id")
        _optional_id(self.order_link_id, "order_link_id")
        if not isinstance(self.symbol, Symbol):
            raise TypeError("symbol must be Symbol")
        if not isinstance(self.qty, Qty) or not isinstance(self.price, Price):
            raise TypeError("execution requires Qty and Price")
        if self.qty.value == 0:
            raise ValueError("execution quantity must be nonzero")
        finite_decimal(self.fee, "fee")  # Negative fees represent rebates.
        require_utc(self.exec_time)
        if not isinstance(self.is_maker, bool):
            raise TypeError("is_maker must be bool")


@dataclass(frozen=True, slots=True)
class OrderEvent:
    order_link_id: str | None
    exchange_order_id: str
    parent_order_link_id: str | None
    state: OrderState
    exchange_cum_exec_qty: Decimal
    event_time: datetime
    raw: Mapping[str, object]
    symbol: Symbol
    order_qty: Qty
    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: Price | None
    trigger_price: Price | None
    reduce_only: bool
    stop_kind: StopKind | None

    def __post_init__(self) -> None:
        _identifier(self.exchange_order_id, "exchange_order_id")
        _optional_id(self.order_link_id, "order_link_id")
        _optional_id(self.parent_order_link_id, "parent_order_link_id")
        if not isinstance(self.state, OrderState):
            raise TypeError("state must be OrderState")
        if finite_decimal(self.exchange_cum_exec_qty, "exchange_cum_exec_qty") < 0:
            raise ValueError("cumulative quantity must be nonnegative")
        require_utc(self.event_time)
        if not isinstance(self.raw, Mapping):
            raise TypeError("raw must be a mapping")
        object.__setattr__(self, "raw", _freeze(self.raw))
        if not isinstance(self.symbol, Symbol) or not isinstance(self.order_qty, Qty):
            raise TypeError("order requires Symbol and Qty")
        if self.order_qty.value == 0:
            raise ValueError("order quantity must be nonzero")
        if not isinstance(self.order_type, OrderType) or not isinstance(
            self.time_in_force, TimeInForce
        ):
            raise TypeError("order type and time in force must be domain enums")
        for value in (self.limit_price, self.trigger_price):
            if value is not None and not isinstance(value, Price):
                raise TypeError("prices must be Price or None")
        if not isinstance(self.reduce_only, bool):
            raise TypeError("reduce_only must be bool")
        if self.stop_kind is not None and not isinstance(self.stop_kind, StopKind):
            raise TypeError("stop_kind must be StopKind or None")
        if (self.order_type is OrderType.LIMIT) != (self.limit_price is not None):
            raise ValueError("only limit orders require a limit price")
        if self.stop_kind is not None and self.trigger_price is None:
            raise ValueError("conditional orders require a trigger price")
        if (
            self.stop_kind in (StopKind.PARTIAL_STOP_LOSS, StopKind.STOP_LOSS)
            and not self.reduce_only
        ):
            raise ValueError("stop loss orders must be reduce-only")
        if (
            self.parent_order_link_id is not None
            and self.stop_kind is not StopKind.PARTIAL_STOP_LOSS
        ):
            raise ValueError("attached stops require PARTIAL_STOP_LOSS")


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    symbol: Symbol
    qty: Qty
    avg_price: Price | None
    snapshot_time: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, Symbol) or not isinstance(self.qty, Qty):
            raise TypeError("position requires Symbol and Qty")
        if self.avg_price is not None and not isinstance(self.avg_price, Price):
            raise TypeError("avg_price must be Price or None")
        if self.qty.value != 0 and self.avg_price is None:
            raise ValueError("a nonflat position requires an average price")
        require_utc(self.snapshot_time)
