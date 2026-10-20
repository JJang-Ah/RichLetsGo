"""Account risk gate: check and reserve in one transaction (D46, D60)."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from richletsgo.domain import (
    Clock,
    Environment,
    InstrumentSpec,
    OrderPurpose,
    OrderState,
    Price,
    Qty,
    StrategyId,
    Symbol,
    make_order_link_id,
)
from richletsgo.domain.values import finite_decimal
from richletsgo.oms.exposure import exposure
from richletsgo.oms.models import OrderRecord
from richletsgo.storage.contracts import UnitOfWork

from .checks import StrategyRiskInput, check_limits, evaluate_pauses
from .equity import EquityTracker
from .limits import RiskLimits
from .pause import PauseState
from .records import (
    DecisionKind,
    DecisionRecord,
    Direction,
    EpisodeRecord,
    EpisodeState,
    Rejection,
    RejectionCode,
)
from .sizing import SizingInput, size_entry


@dataclass(frozen=True, slots=True)
class EntryRequest:
    """One market signal turned into an entry attempt. AI scoring happens before admit."""

    signal_id: str
    strategy: StrategyId
    symbol: Symbol
    direction: Direction
    entry_price: Price
    stop_price: Price
    ai_factor: Decimal
    limit_price: Price | None = None


@dataclass(frozen=True, slots=True)
class MarketInputs:
    """Values read before entering the lock; the gate never calls the exchange itself."""

    marks: Mapping[Symbol, Price]
    instrument: InstrumentSpec
    leverage: Decimal
    mmr: Decimal
    fee_rate: Decimal
    slippage: Decimal


@dataclass(frozen=True, slots=True)
class Admission:
    decision: DecisionRecord
    order: OrderRecord | None
    episode: EpisodeRecord | None
    reused: bool


class RiskGate:
    """One gate per account. Pauses live in memory here; persistence is M1-06/M3."""

    def __init__(
        self,
        *,
        account_id: str,
        environment: Environment,
        uow_factory: Callable[[], UnitOfWork],
        limits: RiskLimits,
        tracker: EquityTracker,
        pauses: PauseState,
        clock: Clock,
        unique_factory: Callable[[], str],
    ) -> None:
        if not account_id:
            raise ValueError("account_id is required")
        self.account_id = account_id
        self.environment = environment
        self._uow = uow_factory
        self.limits = limits
        self.tracker = tracker
        self.pauses = pauses
        self._clock = clock
        self._unique = unique_factory
        self._lock = asyncio.Lock()

    def refresh_pauses(self, strategies: Mapping[StrategyId, StrategyRiskInput]) -> None:
        """Expire timed pauses, then add what the current figures require (D76 step 3)."""
        now = self._clock.now()
        self.pauses.expire(now)
        for pause in evaluate_pauses(self.limits, self.tracker, strategies, now):
            self.pauses.add(pause)

    async def admit(
        self,
        request: EntryRequest,
        market: MarketInputs,
        strategies: Mapping[StrategyId, StrategyRiskInput],
    ) -> Admission:
        if market.instrument.symbol is not request.symbol:
            raise ValueError("instrument does not match the requested symbol")
        finite_decimal(market.leverage, "leverage")
        async with self._lock:
            # Pause refresh is idempotent and derived from inputs, so it is safe even
            # if the transaction below fails; nothing inside the lock awaits I/O.
            self.refresh_pauses(strategies)
            async with self._uow() as uow:
                existing = uow.decisions.get(request.signal_id)
                if existing is not None:
                    order = uow.orders.get(existing.order_key) if existing.order_key else None
                    episode = uow.episodes.active_for(request.strategy, request.symbol)
                    return Admission(existing, order, episode, True)
                admission = self._decide(uow, request, market)
                await uow.commit()
                return admission

    def _decide(self, uow: UnitOfWork, request: EntryRequest, market: MarketInputs) -> Admission:
        now = self._clock.now()

        def record(
            kind: DecisionKind,
            *,
            rejections: tuple[Rejection, ...] = (),
            basis: tuple[tuple[str, str], ...] = (),
            order: OrderRecord | None = None,
        ) -> DecisionRecord:
            decision = DecisionRecord(
                self.account_id,
                request.signal_id,
                request.strategy,
                request.symbol,
                request.direction,
                kind,
                now,
                order.key if order else None,
                order.qty if order else None,
                rejections,
                basis,
            )
            uow.decisions.add(decision)
            return decision

        episode = uow.episodes.active_for(request.strategy, request.symbol)
        if episode is not None:
            if episode.direction is request.direction and episode.state is EpisodeState.OPEN:
                return Admission(record(DecisionKind.MAINTAINED), None, episode, False)
            rejection = Rejection(RejectionCode.EPISODE_OPEN_OPPOSITE, episode.episode_id)
            return Admission(
                record(DecisionKind.REJECTED, rejections=(rejection,)), None, episode, False
            )

        sizing = size_entry(
            SizingInput(
                self.tracker.equity,
                request.direction,
                request.entry_price,
                request.stop_price,
                request.ai_factor,
                market.fee_rate,
                market.slippage,
                market.instrument,
                market.leverage,
                market.mmr,
            ),
            self.limits,
        )
        if not sizing.ok or sizing.qty is None:
            assert sizing.reason is not None
            rejection = Rejection(sizing.reason, "sizing")
            decision = record(DecisionKind.REJECTED, rejections=(rejection,), basis=sizing.basis)
            return Admission(decision, None, None, False)

        notional_price = (
            request.limit_price.value
            if request.limit_price is not None
            else request.entry_price.value * (1 + self.limits.slippage_buffer)
        )
        view = exposure(uow, marks=market.marks, slippage_buffer=self.limits.slippage_buffer)
        check = check_limits(
            limits=self.limits,
            view=view,
            pauses=self.pauses,
            strategy=request.strategy,
            symbol=request.symbol,
            held_for_strategy=uow.positions.get(request.strategy, request.symbol).value,
            order_qty=sizing.qty,
            notional_price=notional_price,
            leverage=market.leverage,
        )
        basis = sizing.basis + (("account_notional_before", str(view.total_notional)),)
        if not check.allowed:
            decision = record(DecisionKind.REJECTED, rejections=check.rejections, basis=basis)
            return Admission(decision, None, None, False)

        link = make_order_link_id(
            self.environment, request.strategy, OrderPurpose.ENTRY, unique_factory=self._unique
        )
        order = OrderRecord(
            link,
            None,
            None,
            request.strategy,
            OrderPurpose.ENTRY,
            request.symbol,
            sizing.qty,
            request.limit_price,
            request.entry_price,
            OrderState.CREATED,
            Qty(Decimal(0)),
            Decimal(0),
            now,
            now,
        )
        uow.orders.put(order)
        episode = EpisodeRecord(
            f"ep-{link}",
            request.strategy,
            request.symbol,
            request.direction,
            sizing.qty,
            now,
            (order.key,),
            EpisodeState.OPEN,
        )
        uow.episodes.put(episode)
        decision = record(DecisionKind.ADMITTED, basis=basis, order=order)
        return Admission(decision, order, episode, False)
