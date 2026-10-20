"""Operational protection policies (V0 rows 33 and 36); callers perform side effects."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from richletsgo.domain import Symbol
from richletsgo.domain.clock import require_utc

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DATA_PAUSE_REASON = "data"


def _floor(ts: datetime, interval: timedelta) -> datetime:
    return _EPOCH + ((ts - _EPOCH) // interval) * interval


@dataclass(frozen=True, slots=True)
class MarketDataStatus:
    """stale=True means: pause new entries for this symbol (ENTRY_PAUSED_SYSTEM 'data')."""

    symbol: Symbol
    stale: bool
    expected_last_bar_start: datetime
    missing_bar_starts: tuple[datetime, ...]


def assess_market_data(
    symbol: Symbol,
    *,
    interval: timedelta,
    last_closed_bar_start: datetime | None,
    now: datetime,
    allowed_delay: timedelta,
) -> MarketDataStatus:
    """The latest bar that must be closed is the one ending at or before now - allowed_delay."""
    require_utc(now)
    if interval <= timedelta(0) or allowed_delay < timedelta(0):
        raise ValueError("interval must be positive and allowed_delay nonnegative")
    expected = _floor(now - allowed_delay, interval) - interval
    if last_closed_bar_start is None:
        return MarketDataStatus(symbol, True, expected, ())
    require_utc(last_closed_bar_start)
    if _floor(last_closed_bar_start, interval) != last_closed_bar_start:
        raise ValueError("bar start is not aligned to the interval")
    missing: list[datetime] = []
    cursor = last_closed_bar_start + interval
    while cursor <= expected:
        missing.append(cursor)
        cursor += interval
    return MarketDataStatus(symbol, bool(missing), expected, tuple(missing))


@dataclass(frozen=True, slots=True)
class CommandAuthorization:
    """Every command result is logged by the caller, including refusals."""

    actor_id: str
    command: str
    at: datetime
    allowed: bool


def authorize_command(
    actor_id: str, command: str, allowed_ids: Collection[str], at: datetime
) -> CommandAuthorization:
    require_utc(at)
    if not command or not command.strip():
        raise ValueError("command is required")
    return CommandAuthorization(actor_id, command, at, bool(actor_id) and actor_id in allowed_ids)
