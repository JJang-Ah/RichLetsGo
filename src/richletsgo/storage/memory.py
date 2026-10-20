"""Serializable, account-scoped in-memory transactions for offline tests."""

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from types import TracebackType
from typing import Self

from richletsgo.domain import Environment, Qty, StrategyId, Symbol
from richletsgo.oms.models import OrderRecord
from richletsgo.risk.records import DecisionRecord, EpisodeRecord, EpisodeState

from .contracts import (
    DecisionRepository,
    EpisodeRepository,
    FillRecord,
    FillRepository,
    LoggedOrderEvent,
    OrderEventLog,
    OrderRepository,
    PositionRepository,
    UnitOfWork,
)


@dataclass
class _State:
    orders: dict[str, OrderRecord] = field(default_factory=dict)
    fills: dict[str, FillRecord] = field(default_factory=dict)
    events: list[LoggedOrderEvent] = field(default_factory=list)
    positions: dict[tuple[StrategyId, Symbol], Qty] = field(default_factory=dict)
    decisions: dict[str, DecisionRecord] = field(default_factory=dict)
    episodes: dict[str, EpisodeRecord] = field(default_factory=dict)

    def clone(self) -> "_State":
        # Values are immutable, including nested raw event payloads.
        return _State(
            self.orders.copy(),
            self.fills.copy(),
            self.events.copy(),
            self.positions.copy(),
            self.decisions.copy(),
            self.episodes.copy(),
        )


class InMemoryDatabase:
    """One database per account; transactions hold its lock until commit/rollback."""

    def __init__(self, account_id: str, environment: Environment) -> None:
        if not account_id or not isinstance(environment, Environment):
            raise ValueError("account and environment are required")
        self.account_id = account_id
        self.environment = environment
        self._state = _State()
        self._lock = asyncio.Lock()

    def unit_of_work(self) -> UnitOfWork:
        return InMemoryUnitOfWork(self)


class _Orders:
    def __init__(self, owner: "InMemoryUnitOfWork") -> None:
        self.owner = owner

    def get(self, key: str) -> OrderRecord | None:
        return self.owner.state.orders.get(key)

    def find(self, order_link_id: str | None, exchange_order_id: str | None) -> OrderRecord | None:
        matches = [
            r
            for r in self.all()
            if (order_link_id is not None and r.order_link_id == order_link_id)
            or (exchange_order_id is not None and r.exchange_order_id == exchange_order_id)
        ]
        if len(matches) > 1:
            raise ValueError("conflicting order identities")
        if not matches:
            return None
        result = matches[0]
        if order_link_id and result.order_link_id and order_link_id != result.order_link_id:
            raise ValueError("conflicting order link ID")
        if (
            exchange_order_id
            and result.exchange_order_id
            and exchange_order_id != result.exchange_order_id
        ):
            raise ValueError("conflicting exchange order ID")
        return result

    def put(self, order: OrderRecord) -> None:
        previous = self.find(order.order_link_id, order.exchange_order_id)
        if previous is not None and previous.key != order.key:
            raise ValueError("cannot change the persistent identity of an order")
        self.owner.state.orders[order.key] = order

    def all(self) -> tuple[OrderRecord, ...]:
        return tuple(self.owner.state.orders.values())


class _Fills:
    def __init__(self, owner: "InMemoryUnitOfWork") -> None:
        self.owner = owner

    def get(self, exec_id: str) -> FillRecord | None:
        return self.owner.state.fills.get(exec_id)

    def add(self, fill: FillRecord) -> None:
        if self.get(fill.event.exec_id) is not None:
            raise ValueError("duplicate exec_id")
        self.owner.state.fills[fill.event.exec_id] = fill

    def replace(self, fill: FillRecord) -> None:
        if self.get(fill.event.exec_id) is None:
            raise KeyError(fill.event.exec_id)
        self.owner.state.fills[fill.event.exec_id] = fill

    def all(self) -> tuple[FillRecord, ...]:
        return tuple(self.owner.state.fills.values())


class _Events:
    def __init__(self, owner: "InMemoryUnitOfWork") -> None:
        self.owner = owner

    def append(self, event: LoggedOrderEvent) -> None:
        self.owner.state.events.append(event)

    def all(self) -> tuple[LoggedOrderEvent, ...]:
        return tuple(self.owner.state.events)


class _Positions:
    def __init__(self, owner: "InMemoryUnitOfWork") -> None:
        self.owner = owner

    def get(self, strategy: StrategyId, symbol: Symbol) -> Qty:
        return self.owner.state.positions.get((strategy, symbol), Qty(Decimal(0)))

    def set(self, strategy: StrategyId, symbol: Symbol, qty: Qty) -> None:
        if (
            not isinstance(strategy, StrategyId)
            or not isinstance(symbol, Symbol)
            or not isinstance(qty, Qty)
        ):
            raise TypeError("position requires domain values")
        self.owner.state.positions[strategy, symbol] = qty

    def all(self) -> tuple[tuple[StrategyId, Symbol, Qty], ...]:
        return tuple(
            (strategy, symbol, qty)
            for (strategy, symbol), qty in self.owner.state.positions.items()
        )


class _Decisions:
    def __init__(self, owner: "InMemoryUnitOfWork") -> None:
        self.owner = owner

    def get(self, signal_id: str) -> DecisionRecord | None:
        return self.owner.state.decisions.get(signal_id)

    def add(self, decision: DecisionRecord) -> None:
        if decision.account_id != self.owner.database.account_id:
            raise ValueError("decision belongs to another account")
        if decision.signal_id in self.owner.state.decisions:
            raise ValueError("duplicate signal_id decision")
        self.owner.state.decisions[decision.signal_id] = decision

    def all(self) -> tuple[DecisionRecord, ...]:
        return tuple(self.owner.state.decisions.values())


class _Episodes:
    def __init__(self, owner: "InMemoryUnitOfWork") -> None:
        self.owner = owner

    def get(self, episode_id: str) -> EpisodeRecord | None:
        return self.owner.state.episodes.get(episode_id)

    def active_for(self, strategy: StrategyId, symbol: Symbol) -> EpisodeRecord | None:
        active = [
            e
            for e in self.owner.state.episodes.values()
            if e.strategy is strategy and e.symbol is symbol and e.state is not EpisodeState.CLOSED
        ]
        if len(active) > 1:
            raise ValueError("multiple active episodes")
        return active[0] if active else None

    def put(self, episode: EpisodeRecord) -> None:
        if episode.state is not EpisodeState.CLOSED:
            current = self.active_for(episode.strategy, episode.symbol)
            if current is not None and current.episode_id != episode.episode_id:
                raise ValueError("another episode is already active for this strategy and symbol")
        self.owner.state.episodes[episode.episode_id] = episode

    def all(self) -> tuple[EpisodeRecord, ...]:
        return tuple(self.owner.state.episodes.values())


class InMemoryUnitOfWork:
    """Explicit commit. Exit without commit, exceptions and cancellation discard all staged state."""

    def __init__(self, database: InMemoryDatabase) -> None:
        self.database = database
        self.environment = database.environment
        self._working: _State | None = None
        self.orders: OrderRepository = _Orders(self)
        self.fills: FillRepository = _Fills(self)
        self.order_events: OrderEventLog = _Events(self)
        self.positions: PositionRepository = _Positions(self)
        self.decisions: DecisionRepository = _Decisions(self)
        self.episodes: EpisodeRepository = _Episodes(self)
        self._entered = False

    @property
    def state(self) -> _State:
        if self._working is None:
            raise RuntimeError("repository access requires an active, uncommitted transaction")
        return self._working

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError("nested/reentrant unit of work is not supported")
        self._entered = True
        try:
            await self.database._lock.acquire()
        except BaseException:
            self._entered = False
            raise
        self._working = self.database._state.clone()
        return self

    async def commit(self) -> None:
        # No await between publication and clearing staged state.
        self.database._state = self.state.clone()
        self._working = None

    async def rollback(self) -> None:
        self._working = None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            await self.rollback()
        finally:
            self._entered = False
            self.database._lock.release()
