"""Pure recovery decisions. Empty lookups never prove an order absent."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from richletsgo.domain import OrderEvent, OrderPurpose, OrderState
from richletsgo.domain.clock import require_utc

from .models import OrderRecord, apply_order_event


class RecoveryAction(StrEnum):
    KEEP_UNKNOWN = "KEEP_UNKNOWN"
    MOVE_TO = "MOVE_TO"
    SEND_CANCEL = "SEND_CANCEL"
    ALERT_MANUAL_CHECK = "ALERT_MANUAL_CHECK"


class CancelResult(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    ACCEPTED = "ACCEPTED"
    NOT_FOUND = "NOT_FOUND"
    TOO_LATE = "TOO_LATE"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class LookupResult:
    success: bool
    event: OrderEvent | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise TypeError("success must be bool")
        if not self.success and self.event is not None:
            raise ValueError("failed lookup cannot carry a confirmed order")


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    action: RecoveryAction
    record: OrderRecord
    alerted: bool


def resolve_unknown(
    record: OrderRecord,
    results: tuple[LookupResult, ...],
    cancel_result: CancelResult,
    *,
    now: datetime,
    unknown_since: datetime,
    alert_after: timedelta,
    alerted: bool,
    last_cancel_at: datetime | None,
    cancel_retry_after: timedelta,
) -> RecoveryDecision:
    """Caller persists alerted and actual cancel-attempt time; acknowledgement is not terminal."""
    require_utc(now)
    require_utc(unknown_since)
    if now < unknown_since or alert_after <= timedelta(0):
        raise ValueError("invalid recovery timing")
    if record.state is not OrderState.UNKNOWN:
        raise ValueError("recovery requires UNKNOWN")
    if not isinstance(cancel_result, CancelResult):
        raise TypeError("cancel_result must be CancelResult")
    if cancel_retry_after <= timedelta(0):
        raise ValueError("cancel_retry_after must be positive")
    if last_cancel_at is not None:
        require_utc(last_cancel_at)
        if last_cancel_at > now:
            raise ValueError("last_cancel_at is in the future")
    if cancel_result is not CancelResult.NOT_ATTEMPTED and last_cancel_at is None:
        raise ValueError("a previous cancel result requires last_cancel_at")
    current = record
    found = sorted(
        (r.event for r in results if r.success and r.event is not None),
        key=lambda event: event.event_time,
    )
    for event in found:
        if event.state in (
            OrderState.CREATED,
            OrderState.SUBMITTED,
            OrderState.UNKNOWN,
            OrderState.NOT_FOUND,
            OrderState.ABORTED,
        ):
            continue
        current, _ = apply_order_event(current, event)
    if current.state is not OrderState.UNKNOWN:
        return RecoveryDecision(RecoveryAction.MOVE_TO, current, alerted)
    if now - unknown_since >= alert_after and not alerted:
        return RecoveryDecision(RecoveryAction.ALERT_MANUAL_CHECK, current, True)
    retryable = cancel_result in (
        CancelResult.NOT_ATTEMPTED,
        CancelResult.NOT_FOUND,
        CancelResult.TOO_LATE,
        CancelResult.ERROR,
    )
    due = last_cancel_at is None or now - last_cancel_at >= cancel_retry_after
    if record.purpose is OrderPurpose.ENTRY and retryable and due:
        return RecoveryDecision(RecoveryAction.SEND_CANCEL, current, alerted)
    return RecoveryDecision(RecoveryAction.KEEP_UNKNOWN, current, alerted)
