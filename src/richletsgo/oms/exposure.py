"""Conservative entry exposure: stops/close orders never free capacity early."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from richletsgo.domain import Price, StrategyId, Symbol
from richletsgo.domain.clock import require_utc
from richletsgo.domain.values import finite_decimal
from richletsgo.storage.contracts import UnitOfWork


@dataclass(frozen=True, slots=True)
class SymbolExposure:
    symbol: Symbol
    held_qty: Decimal
    unbooked_qty: Decimal
    reserved_qty: Decimal
    held_notional: Decimal
    unbooked_notional: Decimal
    reserved_notional: Decimal

    @property
    def total_notional(self) -> Decimal:
        return self.held_notional + self.unbooked_notional + self.reserved_notional


@dataclass(frozen=True, slots=True)
class ExposureView:
    symbols: tuple[SymbolExposure, ...]

    @property
    def total_notional(self) -> Decimal:
        return sum((item.total_notional for item in self.symbols), Decimal(0))


def exposure(
    uow: UnitOfWork,
    *,
    marks: Mapping[Symbol, Price],
    slippage_buffer: Decimal,
    symbol: Symbol | None = None,
    strategy: StrategyId | None = None,
) -> ExposureView:
    """Read a transactional snapshot. Current marks and slippage are explicit inputs."""
    if finite_decimal(slippage_buffer, "slippage_buffer") < 0:
        raise ValueError("slippage_buffer must be nonnegative")
    orders = [o for o in uow.orders.all() if strategy is None or o.strategy is strategy]
    positions = [p for p in uow.positions.all() if strategy is None or p[0] is strategy]
    selected = {symbol} if symbol else {o.symbol for o in orders} | {p[1] for p in positions}
    rows = []
    for contract in sorted(selected, key=lambda item: item.value):
        held = sum((q.value for _, sym, q in positions if sym is contract), Decimal(0))
        relevant = [o for o in orders if o.symbol is contract]
        unbooked = sum((o.unbooked_qty for o in relevant), Decimal(0))
        reserved = sum((o.reserved_qty for o in relevant), Decimal(0))
        mark = marks.get(contract)
        if held and mark is None:
            raise ValueError(f"missing current mark for {contract}")
        held_notional = held.copy_abs() * mark.value if mark is not None else Decimal(0)
        # Sum absolute entry reservations per order: opposite pending entries do not offset.
        rows.append(
            SymbolExposure(
                contract,
                held,
                unbooked,
                reserved,
                held_notional,
                sum(
                    (
                        o.unbooked_qty.copy_abs() * o.conservative_price(slippage_buffer)
                        for o in relevant
                    ),
                    Decimal(0),
                ),
                sum(
                    (
                        o.reserved_qty.copy_abs() * o.conservative_price(slippage_buffer)
                        for o in relevant
                    ),
                    Decimal(0),
                ),
            )
        )
    return ExposureView(tuple(rows))


@dataclass(frozen=True, slots=True)
class UnbookedWork:
    order_keys: tuple[str, ...]
    paused_symbols: frozenset[Symbol]


def unbooked_work(
    uow: UnitOfWork, *, first_seen: Mapping[str, datetime], now: datetime, timeout: timedelta
) -> UnbookedWork:
    """Caller persists first-seen times, so repeated order updates cannot reset the deadline."""
    require_utc(now)
    if timeout <= timedelta(0):
        raise ValueError("timeout must be positive")
    due = []
    for order in uow.orders.all():
        if order.exchange_cum_exec_qty <= order.booked_qty.value.copy_abs():
            continue
        since = first_seen.get(order.key)
        if since is not None and now - require_utc(since) >= timeout:
            due.append(order)
    return UnbookedWork(tuple(o.key for o in due), frozenset(o.symbol for o in due))
