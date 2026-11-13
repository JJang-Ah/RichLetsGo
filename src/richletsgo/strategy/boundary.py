"""Shared deterministic D76 ordering, independent of input transport and persistence."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from richletsgo.domain import Symbol
from richletsgo.marketdata.models import Interval
from richletsgo.strategy.models import S2PParams


async def boundary_steps(
    fills: Callable[[], Awaitable[None]],
    funding: Callable[[], Awaitable[None]],
    account: Callable[[], Awaitable[None]],
    signals: Callable[[], Awaitable[None]],
) -> None:
    await fills()
    await funding()
    await account()
    await signals()


async def boundary_signals(
    at: datetime,
    symbols: tuple[Symbol, ...],
    s2: Any,
    first: Callable[[Symbol], Awaitable[None]],
    second: Callable[[Symbol], Awaitable[None]],
    settle: Callable[[], Awaitable[None]],
) -> None:
    if Interval.DAY.aligned(at):
        for symbol in symbols:
            await first(symbol)
    if s2 is None:
        return
    if not isinstance(s2, S2PParams) or Interval.FOUR_HOUR.aligned(at):
        for symbol in symbols:
            await second(symbol)
    await settle()
