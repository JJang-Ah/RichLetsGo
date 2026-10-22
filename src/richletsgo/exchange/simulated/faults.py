"""Declarative one-shot faults; counters belong to each simulated exchange instance."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from richletsgo.domain.values import finite_decimal


class FaultKind(StrEnum):
    AMEND_UNSUPPORTED = "AMEND_UNSUPPORTED"
    RESPONSE_LOST = "RESPONSE_LOST"
    HIDE_ORDER = "HIDE_ORDER"
    QUERY_ERROR = "QUERY_ERROR"
    ORDER_FIRST = "ORDER_FIRST"
    DUPLICATE = "DUPLICATE"
    DISCONNECT = "DISCONNECT"
    HOLD_ATTACHED = "HOLD_ATTACHED"
    PARTIAL_FILL = "PARTIAL_FILL"
    REJECT = "REJECT"
    CANCEL_NOT_FOUND = "CANCEL_NOT_FOUND"
    CANCEL_TOO_LATE = "CANCEL_TOO_LATE"
    CANCEL_RACE = "CANCEL_RACE"


@dataclass(frozen=True, slots=True)
class Fault:
    operation: str
    occurrence: int
    kind: FaultKind
    order_link_id: str | None = None
    value: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        operations = {
            "place",
            "cancel",
            "amend",
            "fill",
            "open_orders",
            "history",
            "executions",
            "positions",
            "account",
            "instrument",
        }
        allowed = {
            FaultKind.AMEND_UNSUPPORTED: {"amend"},
            FaultKind.RESPONSE_LOST: {"place", "cancel", "amend"},
            FaultKind.HIDE_ORDER: {"place"},
            FaultKind.ORDER_FIRST: {"fill"},
            FaultKind.DUPLICATE: {"fill"},
            FaultKind.HOLD_ATTACHED: {"place"},
            FaultKind.PARTIAL_FILL: {"fill"},
            FaultKind.REJECT: {"place", "fill"},
            FaultKind.CANCEL_NOT_FOUND: {"cancel"},
            FaultKind.CANCEL_TOO_LATE: {"cancel"},
            FaultKind.CANCEL_RACE: {"cancel"},
            FaultKind.QUERY_ERROR: operations - {"place", "cancel", "amend", "fill"},
            FaultKind.DISCONNECT: operations,
        }
        if (
            self.operation not in operations
            or self.occurrence < 1
            or self.operation not in allowed[self.kind]
        ):
            raise ValueError("invalid fault operation/occurrence")
        finite_decimal(self.value, "fault value")
        if self.kind is FaultKind.PARTIAL_FILL and not 0 <= self.value <= 1:
            raise ValueError("partial fill fraction must be in [0, 1]")
        if self.kind is FaultKind.HIDE_ORDER and (self.value < 0 or self.value != int(self.value)):
            raise ValueError("hidden query count must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class FaultPlan:
    faults: tuple[Fault, ...] = ()
