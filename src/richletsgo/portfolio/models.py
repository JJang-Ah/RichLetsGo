"""Immutable account book, protection and close-intent records (D41/D42/D64)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from richletsgo.domain import Price, Qty, StrategyId, Symbol
from richletsgo.domain.clock import require_utc
from richletsgo.domain.values import finite_decimal


@dataclass(frozen=True, slots=True)
class StopParameters:
    stop_loss: Price
    trigger_by: str

    def __post_init__(self) -> None:
        if not isinstance(self.stop_loss, Price):
            raise TypeError("a stop price is required")
        if self.trigger_by not in ("LastPrice", "MarkPrice", "IndexPrice"):
            raise ValueError("invalid stop trigger source")

    def payload(self) -> dict[str, str]:
        return {
            "stopLoss": str(self.stop_loss.value),
            "tpslMode": "Partial",
            "slOrderType": "Market",
            "slTriggerBy": self.trigger_by,
        }


@dataclass(frozen=True, slots=True)
class StopLink:
    order_key: str
    qty: Decimal  # absolute confirmed remaining quantity


@dataclass(frozen=True, slots=True)
class StrategyBook:
    strategy: StrategyId
    symbol: Symbol
    qty: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    realized_pnl: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    funding: Decimal = Decimal(0)  # positive = cost, negative = receipt
    stop_price: Price | None = None
    stops: tuple[StopLink, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, StrategyId) or not isinstance(self.symbol, Symbol):
            raise TypeError("book requires domain strategy and symbol")
        for name in ("qty", "realized_pnl", "fees", "funding"):
            finite_decimal(getattr(self, name), name)
        if self.qty and (
            self.avg_price is None or finite_decimal(self.avg_price, "avg_price") <= 0
        ):
            raise ValueError("nonflat book requires positive average price")
        if not self.qty and self.avg_price is not None:
            raise ValueError("flat book must not retain an average price")
        if len({s.order_key for s in self.stops}) != len(self.stops):
            raise ValueError("duplicate stop identity")
        if any(finite_decimal(s.qty, "stop qty") < 0 for s in self.stops):
            raise ValueError("stop qty must be nonnegative")

    @property
    def net_realized(self) -> Decimal:
        return self.realized_pnl - self.fees - self.funding


class CloseState(StrEnum):
    RECORDED = "RECORDED"
    CLOSING = "CLOSING"
    FLAT_CONFIRMED = "FLAT_CONFIRMED"
    PROTECTING_OTHERS = "PROTECTING_OTHERS"
    STOPS_CANCELLING = "STOPS_CANCELLING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"


@dataclass(frozen=True, slots=True)
class CloseIntent:
    intent_id: str
    episode_id: str
    strategy: StrategyId
    symbol: Symbol
    created_at: datetime
    state: CloseState = CloseState.RECORDED
    close_attempts: int = 0
    protect_attempts: tuple[tuple[StrategyId, int], ...] = ()
    last_attempt_at: datetime | None = None
    snapshot_revision: int = -1

    def __post_init__(self) -> None:
        if not self.intent_id or not self.episode_id:
            raise ValueError("intent and episode IDs required")
        require_utc(self.created_at)
        if self.last_attempt_at is not None:
            require_utc(self.last_attempt_at)
        if self.close_attempts < 0 or any(n < 0 for _, n in self.protect_attempts):
            raise ValueError("invalid attempt count")

    def to_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "episode_id": self.episode_id,
            "strategy": self.strategy.value,
            "symbol": self.symbol.value,
            "created_at": self.created_at.isoformat(),
            "state": self.state.value,
            "close_attempts": self.close_attempts,
            "protect_attempts": [(s.value, n) for s, n in self.protect_attempts],
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "snapshot_revision": self.snapshot_revision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "CloseIntent":
        attempts = data["protect_attempts"]
        if not isinstance(attempts, list):
            raise ValueError("protect_attempts must be a list")
        return cls(
            str(data["intent_id"]),
            str(data["episode_id"]),
            StrategyId(str(data["strategy"])),
            Symbol(str(data["symbol"])),
            require_utc(datetime.fromisoformat(str(data["created_at"]))),
            CloseState(str(data["state"])),
            int(str(data["close_attempts"])),
            tuple((StrategyId(s), int(n)) for s, n in attempts),
            require_utc(datetime.fromisoformat(str(data["last_attempt_at"])))
            if data["last_attempt_at"]
            else None,
            int(str(data["snapshot_revision"])),
        )


class ActionKind(StrEnum):
    CANCEL_ENTRY = "CANCEL_ENTRY"
    CLOSE = "CLOSE"
    PROTECT = "PROTECT"
    REDUCE_UNPROTECTED = "REDUCE_UNPROTECTED"
    CANCEL_STOP = "CANCEL_STOP"
    TRIM_STOP = "TRIM_STOP"
    QUERY_STOPS = "QUERY_STOPS"
    ALERT = "ALERT"
    PAUSE = "PAUSE"


@dataclass(frozen=True, slots=True)
class Action:
    key: str
    kind: ActionKind
    strategy: StrategyId
    symbol: Symbol
    qty: Decimal = Decimal(0)  # signed order quantity; TRIM_STOP is remaining absolute qty
    order_key: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ClosePolicy:
    max_close_attempts: int
    max_protect_attempts: int
    retry_after: timedelta
    trigger_by: str

    def __post_init__(self) -> None:
        if any(
            type(n) is not int or n < 1
            for n in (self.max_close_attempts, self.max_protect_attempts)
        ):
            raise ValueError("attempt counts must be positive")
        if self.retry_after <= timedelta(0):
            raise ValueError("retry interval must be positive")
        StopParameters(Price(Decimal(1)), self.trigger_by)


def entry_delta(target: Qty, held: Qty, reserved: Qty, unbooked: Qty) -> Qty:
    """D53 signed difference. Never cross zero or reduce through an entry order."""
    for q in (held, reserved, unbooked):
        if q.value * target.value < 0:
            return Qty(Decimal(0))
    delta = target.value - held.value - reserved.value - unbooked.value
    return Qty(delta if delta * target.value > 0 else Decimal(0))


def apply_book_fill(book: StrategyBook, qty: Decimal, price: Decimal, fee: Decimal) -> StrategyBook:
    from dataclasses import replace

    finite_decimal(qty, "qty")
    if not qty:
        return book
    new_qty = book.qty + qty
    average: Decimal | None
    realized = book.realized_pnl
    if book.qty * qty >= 0:
        average = (abs(book.qty) * (book.avg_price or Decimal(0)) + abs(qty) * price) / abs(new_qty)
    else:
        if abs(qty) > abs(book.qty):
            raise ValueError("allocate overflow before applying a reduction")
        assert book.avg_price is not None
        realized += abs(qty) * (price - book.avg_price) * (1 if book.qty > 0 else -1)
        average = book.avg_price if new_qty else None
    return replace(
        book, qty=new_qty, avg_price=average, realized_pnl=realized, fees=book.fees + fee
    )
