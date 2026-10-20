"""UTC-only clocks with no dependency on wall-clock time."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


def require_utc(value: datetime) -> datetime:
    """Accept aware timestamps with zero UTC offset; never silently convert."""
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC aware")
    return value


class Clock(Protocol):
    def now(self) -> datetime:
        """Return an aware UTC timestamp."""
        ...


@dataclass(frozen=True, slots=True)
class FixedClock:
    instant: datetime

    def __post_init__(self) -> None:
        require_utc(self.instant)

    def now(self) -> datetime:
        return self.instant


class ManualClock:
    """Explicit time control for deterministic simulations and tests."""

    __slots__ = ("_instant",)

    def __init__(self, instant: datetime) -> None:
        self._instant = require_utc(instant)

    def now(self) -> datetime:
        return self._instant

    def set(self, instant: datetime) -> None:
        self._instant = require_utc(instant)

    def advance(self, delta: timedelta) -> None:
        if not isinstance(delta, timedelta):
            raise TypeError("delta must be timedelta")
        self.set(self._instant + delta)


def trading_day_start(ts: datetime) -> datetime:
    """Return the UTC midnight starting the timestamp's trading day."""
    return require_utc(ts).replace(hour=0, minute=0, second=0, microsecond=0)
