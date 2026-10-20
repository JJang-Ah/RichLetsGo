"""Risk-gate records persisted with orders; this module must not import storage."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from richletsgo.domain import Qty, StrategyId, Symbol
from richletsgo.domain.clock import require_utc


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1


class DecisionKind(StrEnum):
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"
    MAINTAINED = "MAINTAINED"


class RejectionCode(StrEnum):
    PAUSED = "PAUSED"
    INVALID_STOP = "INVALID_STOP"
    BELOW_MIN_QTY = "BELOW_MIN_QTY"
    LEVERAGE_OUT_OF_RANGE = "LEVERAGE_OUT_OF_RANGE"
    LIQUIDATION_TOO_CLOSE = "LIQUIDATION_TOO_CLOSE"
    L2_SYMBOL_NOTIONAL = "L2_SYMBOL_NOTIONAL"
    L3_ACCOUNT_NOTIONAL = "L3_ACCOUNT_NOTIONAL"
    EPISODE_OPEN_OPPOSITE = "EPISODE_OPEN_OPPOSITE"


@dataclass(frozen=True, slots=True)
class Rejection:
    code: RejectionCode
    detail: str


type Basis = tuple[tuple[str, str], ...]


def signal_key(strategy: StrategyId, symbol: Symbol, bar_start: datetime, kind: str) -> str:
    """Stable decision key for (strategy, contract, bar start, signal kind) — D43."""
    if not kind or not kind.strip() or "|" in kind:
        raise ValueError("signal kind must be nonempty and contain no '|'")
    return f"{strategy.value}|{symbol.value}|{require_utc(bar_start).isoformat()}|{kind}"


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One decision per (account, signal_id) — D60. Rejections are stored too."""

    account_id: str
    signal_id: str
    strategy: StrategyId
    symbol: Symbol
    direction: Direction
    kind: DecisionKind
    decided_at: datetime
    order_key: str | None
    approved_qty: Qty | None
    rejections: tuple[Rejection, ...]
    basis: Basis

    def __post_init__(self) -> None:
        if not self.account_id or not self.signal_id:
            raise ValueError("account and signal IDs are required")
        require_utc(self.decided_at)
        admitted = self.kind is DecisionKind.ADMITTED
        if admitted != (self.order_key is not None) or admitted != (self.approved_qty is not None):
            raise ValueError("only admitted decisions carry an order and quantity")
        if (self.kind is DecisionKind.REJECTED) != bool(self.rejections):
            raise ValueError("rejected decisions, and only those, carry rejections")


class EpisodeState(StrEnum):
    OPEN = "OPEN"
    EXITING = "EXITING"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class EpisodeRecord:
    """Entry episode — D59. M1-03 opens episodes; exit and close rules belong to M1-04."""

    episode_id: str
    strategy: StrategyId
    symbol: Symbol
    direction: Direction
    approved_qty: Qty
    opened_at: datetime
    entry_order_keys: tuple[str, ...]
    state: EpisodeState

    def __post_init__(self) -> None:
        if not self.episode_id:
            raise ValueError("episode_id is required")
        require_utc(self.opened_at)
        if self.approved_qty.value == 0 or (self.approved_qty.value > 0) != (
            self.direction is Direction.LONG
        ):
            raise ValueError("approved quantity must be nonzero and match the direction")
