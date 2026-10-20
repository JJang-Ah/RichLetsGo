"""Shared domain vocabulary; transition logic belongs to OMS."""

from enum import StrEnum


class Symbol(StrEnum):
    BTCUSDT = "BTCUSDT"
    ETHUSDT = "ETHUSDT"


class StrategyId(StrEnum):
    S1 = "S1"
    S2 = "S2"


class Environment(StrEnum):
    BACKTEST = "backtest"
    DEMO = "demo"
    LIVE = "live"


class OrderPurpose(StrEnum):
    ENTRY = "E"
    CLOSE = "X"
    PROTECT = "P"
    ATTACHED_SL = "ATTACHED_SL"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderState(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    NOT_FOUND = "NOT_FOUND"
    ABORTED = "ABORTED"

    @property
    def rank(self) -> int:
        """Terminal states share a rank; rank alone cannot validate a transition."""
        return {
            OrderState.CREATED: 0,
            OrderState.SUBMITTED: 1,
            OrderState.UNKNOWN: 2,
            OrderState.OPEN: 3,
            OrderState.PARTIALLY_FILLED: 4,
        }.get(self, 5)

    @property
    def is_terminal(self) -> bool:
        return self.rank == 5
