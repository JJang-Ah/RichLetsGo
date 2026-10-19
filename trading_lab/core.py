from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Market(str, Enum):
    KR = "kis_kr"
    US = "kis_us"
    FUTURES = "bybit_usdt_linear"


class Rejected(ValueError):
    pass


def number(value: Decimal, name: str, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise Rejected(f"{name}: finite Decimal required")
    if positive and value <= 0:
        raise Rejected(f"{name}: must be positive")


@dataclass(frozen=True)
class Intent:
    market: Market
    symbol: str
    currency: str
    side: str
    quantity: Decimal
    reference_price: Decimal
    leverage: Decimal = Decimal("1")
    reduce_only: bool = False


@dataclass(frozen=True)
class Limits:
    max_order_notional: Decimal
    max_position_notional: Decimal
    max_leverage: Decimal


def validate(intent: Intent, limits: Limits, *, position: Decimal = Decimal("0"),
             halted: bool = False) -> Decimal:
    """Return prospective signed position. Does not submit, fill or reserve orders."""
    if type(halted) is not bool or type(intent.reduce_only) is not bool:
        raise Rejected("flags: bool required")
    if halted:
        raise Rejected("offline validation halted")
    if not isinstance(intent.market, Market):
        raise Rejected("unsupported market")
    if not isinstance(intent.symbol, str) or not intent.symbol.strip():
        raise Rejected("symbol required")
    currencies = {Market.KR: "KRW", Market.US: "USD", Market.FUTURES: "USDT"}
    if intent.currency != currencies[intent.market]:
        raise Rejected("currency does not match market")
    if intent.side not in ("buy", "sell"):
        raise Rejected("side must be buy or sell")
    for name in ("quantity", "reference_price", "leverage"):
        number(getattr(intent, name), name, positive=True)
    for name in ("max_order_notional", "max_position_notional", "max_leverage"):
        number(getattr(limits, name), name, positive=True)
    number(position, "position")
    if intent.leverage < 1 or intent.leverage > limits.max_leverage:
        raise Rejected("leverage outside configured range")
    if intent.market != Market.FUTURES:
        if intent.leverage != 1 or intent.reduce_only:
            raise Rejected("stock leverage/reduce-only unsupported")
        if intent.quantity != intent.quantity.to_integral_value():
            raise Rejected("stock quantity must be whole shares")
        if position < 0 or position != position.to_integral_value():
            raise Rejected("invalid stock position")
    delta = intent.quantity if intent.side == "buy" else -intent.quantity
    next_position = position + delta
    if intent.reduce_only:
        if position == 0 or position * delta >= 0 or abs(delta) > abs(position):
            raise Rejected("reduce-only must decrease an existing position without reversal")
    if intent.market != Market.FUTURES and next_position < 0:
        raise Rejected("stock short selling unsupported")
    if intent.quantity * intent.reference_price > limits.max_order_notional:
        raise Rejected("order notional exceeds limit")
    # Risk-reducing orders may reduce an already oversized position.
    reducing = position * delta < 0 and abs(delta) <= abs(position)
    if not reducing and abs(next_position) * intent.reference_price > limits.max_position_notional:
        raise Rejected("position notional exceeds limit")
    return next_position
