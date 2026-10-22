"""Transactional trailing-stop planning and journal-backed attempt recovery.

No exchange I/O. The coordinator supplies successful REST observation boundaries.
"""

import json
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from richletsgo.domain import OrderPurpose, OrderState, Price, Symbol
from richletsgo.domain.clock import require_utc
from richletsgo.oms.models import OrderRecord
from richletsgo.risk.records import EpisodeState
from richletsgo.storage.contracts import UnitOfWork

from .models import Action, ActionKind, ClosePolicy, CloseState

if TYPE_CHECKING:
    from richletsgo.oms.journal import JournalRecord


def favorable(candidate: Decimal, current: Decimal, qty: Decimal) -> bool:
    return candidate > current if qty > 0 else candidate < current


def target_text(target: Price) -> str:
    text = format(target.value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True, slots=True)
class TrailRecovery:
    order_key: str
    target: Price
    attempts: int = 0
    last_attempt_at: datetime | None = None
    in_flight: str | None = None

    @property
    def key(self) -> str:
        return f"trail:{self.order_key}:{target_text(self.target)}"

    @property
    def action_key(self) -> str:
        return f"amend:{self.order_key}:{target_text(self.target)}:{self.attempts}"

    def encode(self) -> str:
        return json.dumps(
            {
                "order_key": self.order_key,
                "target": target_text(self.target),
                "attempts": self.attempts,
                "last_attempt_at": self.last_attempt_at.isoformat()
                if self.last_attempt_at
                else None,
                "in_flight": self.in_flight,
            }
        )

    @classmethod
    def decode(cls, value: str) -> "TrailRecovery":
        data = json.loads(value)
        return cls(
            data["order_key"],
            Price(Decimal(data["target"])),
            data["attempts"],
            require_utc(datetime.fromisoformat(data["last_attempt_at"]))
            if data["last_attempt_at"]
            else None,
            data["in_flight"],
        )


def recoveries(uow: UnitOfWork) -> tuple[TrailRecovery, ...]:
    return tuple(TrailRecovery.decode(v) for k, v in uow.recovery.items() if k.startswith("trail:"))


def recovery(uow: UnitOfWork, order_key: str, target: Price) -> TrailRecovery:
    empty = TrailRecovery(order_key, target)
    raw = uow.recovery.get(empty.key)
    return TrailRecovery.decode(raw) if raw else empty


def closing_symbol(uow: UnitOfWork, symbol: Symbol) -> bool:
    return any(
        i.symbol is symbol and i.state is not CloseState.DONE for i in uow.portfolio.intents()
    )


def can_amend(uow: UnitOfWork, order: OrderRecord, target: Price) -> bool:
    book = uow.portfolio.book(order.strategy, order.symbol)
    episode = uow.episodes.active_for(order.strategy, order.symbol)
    return bool(
        episode is not None
        and episode.state is EpisodeState.OPEN
        and book.qty
        and book.stop_episode_id == episode.episode_id
        and not closing_symbol(uow, order.symbol)
        and order.purpose in (OrderPurpose.ATTACHED_SL, OrderPurpose.PROTECT)
        and order.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED)
        and order.qty.value * book.qty < 0
        and order.trigger_price is not None
        and book.stop_price is not None
        and favorable(target.value, order.trigger_price.value, book.qty)
        and target == book.stop_price
        and not any(r.order_key == order.key and r.in_flight for r in recoveries(uow))
    )


def align_stops(uow: UnitOfWork, now: datetime, policy: ClosePolicy) -> None:
    for order in uow.orders.all():
        book = uow.portfolio.book(order.strategy, order.symbol)
        target = book.stop_price
        if target is None or not can_amend(uow, order, target):
            continue
        state = recovery(uow, order.key, target)
        if state.attempts >= policy.max_protect_attempts:
            uow.portfolio.pause(order.symbol)
            uow.portfolio.emit(
                Action(
                    state.key + ":exhausted",
                    ActionKind.ALERT,
                    order.strategy,
                    order.symbol,
                    detail="trailing stop retries exhausted; existing stop retained",
                )
            )
        elif state.last_attempt_at is None or now - state.last_attempt_at >= policy.retry_after:
            uow.portfolio.emit(
                Action(
                    state.action_key,
                    ActionKind.AMEND_STOP,
                    order.strategy,
                    order.symbol,
                    order_key=order.key,
                    trigger=target,
                )
            )


def ingest_trail_record(uow: UnitOfWork, record: "JournalRecord") -> None:
    """Idempotently take responsibility for an attempt, including DB-outage replay."""
    payload = record.payload()
    trail = payload.get("trail")
    if record.op_type != "amend" or trail is None:
        return
    state = recovery(uow, trail["order_key"], Price(Decimal(payload["after"]["trigger"])))
    seen_key = "trail_seen:" + record.operation_id
    if not uow.recovery.get(seen_key):
        # Results carry the same request payload; tolerate replay order as well.
        state = replace(
            state,
            attempts=max(state.attempts, int(trail["attempt"]) + 1),
            last_attempt_at=require_utc(datetime.fromisoformat(trail["requested_at"])),
            in_flight=record.operation_id,
        )
        uow.recovery.put(seen_key, "owned")
    if record.phase == "result" and state.in_flight == record.operation_id:
        result = json.loads(record.result)
        if result.get("code") in ("REJECTED", "UNSUPPORTED", "NOT_FOUND", "TOO_LATE"):
            state = replace(state, in_flight=None)
    uow.recovery.put(state.key, state.encode())


def settle_trails(
    uow: UnitOfWork,
    now: datetime,
    policy: ClosePolicy,
    *,
    queried: frozenset[str] = frozenset(),
    query_started: datetime | None = None,
) -> None:
    for state in recoveries(uow):
        if state.in_flight is None:
            continue
        order = uow.orders.get(state.order_key)
        if order is None:
            continue
        outcome = None
        if order.state.is_terminal:
            outcome = "SUPERSEDED"
        elif order.trigger_price == state.target:
            outcome = "APPLIED"
        elif (
            order.key in queried
            and query_started is not None
            and state.last_attempt_at is not None
            and query_started - state.last_attempt_at >= policy.retry_after
        ):
            raw = uow.recovery.get("journal:" + state.in_flight + ":requested")
            if raw:
                before = json.loads(json.loads(raw)["request"])["before"]["trigger"]
                outcome = (
                    "NOT_APPLIED"
                    if order.trigger_price
                    and before is not None
                    and order.trigger_price.value == Decimal(before)
                    else "SUPERSEDED"
                )
        if outcome:
            uow.recovery.put("outcome:" + state.in_flight, outcome)
            uow.recovery.put(state.key, replace(state, in_flight=None).encode())
