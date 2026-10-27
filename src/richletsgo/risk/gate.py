"""Account risk gate: check and reserve in one transaction (D46, D60)."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
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
from richletsgo.domain.clock import require_utc
from richletsgo.domain.values import finite_decimal
from richletsgo.oms.exposure import exposure
from richletsgo.oms.models import OrderRecord
from richletsgo.portfolio.models import CloseState, StopParameters, entry_delta
from richletsgo.storage.contracts import FillStatus, UnitOfWork

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
    stop_trigger_by: str = "LastPrice"
    ai_mode: str = "disabled"
    ai_score: Decimal | None = None
    ai_status: str = "DISABLED"
    entry_rejection: Rejection | None = None
    is_exit: bool = False
    expires_at: datetime | None = None
    post_only: bool = False

    def __post_init__(self) -> None:
        StopParameters(self.stop_price, self.stop_trigger_by)
        if self.expires_at is not None:
            require_utc(self.expires_at)
        if type(self.post_only) is not bool or (self.post_only and self.limit_price is None):
            raise ValueError("post_only requires a limit order")


@dataclass(frozen=True, slots=True)
class MarketInputs:
    """Values read before entering the lock; the gate never calls the exchange itself."""

    marks: Mapping[Symbol, Price]
    instrument: InstrumentSpec
    leverage: Decimal
    mmr: Decimal
    fee_rate: Decimal
    slippage: Decimal
    tier_notional_limit: Decimal | None = None

    def __post_init__(self) -> None:
        if self.tier_notional_limit is not None:
            if finite_decimal(self.tier_notional_limit, "tier_notional_limit") <= 0:
                raise ValueError("tier notional limit must be positive")


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
        *,
        prepare: Callable[[UnitOfWork], None] | None = None,
        after_decision: Callable[[UnitOfWork, Admission], None] | None = None,
    ) -> Admission:
        if market.instrument.symbol is not request.symbol:
            raise ValueError("instrument does not match the requested symbol")
        finite_decimal(market.leverage, "leverage")
        async with self._lock:
            async with self._uow() as uow:
                # UoW acquisition may wait. Read current risk inputs only after that wait.
                self.refresh_pauses(strategies)
                existing = uow.decisions.get(request.signal_id)
                if existing is not None:
                    order = uow.orders.get(existing.order_key) if existing.order_key else None
                    episode = uow.episodes.active_for(request.strategy, request.symbol)
                    return Admission(existing, order, episode, True)
                if prepare:
                    prepare(uow)
                if (
                    uow.episodes.active_for(request.strategy, request.symbol) is None
                    and not request.is_exit
                    and request.entry_rejection is None
                    and self.limits.strategy_daily_limit(request.strategy) > 0
                    and request.strategy not in strategies
                ):
                    raise ValueError("requested strategy risk input is required")
                admission = self._decide(uow, request, market)
                if after_decision:
                    after_decision(uow, admission)
                await uow.commit()
                return admission

    def _decide(self, uow: UnitOfWork, request: EntryRequest, market: MarketInputs) -> Admission:
        now = self._clock.now()
        original_qty: Qty | None = None
        rejection: Rejection | None

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
                original_qty,
                request.ai_mode,
                request.ai_score,
                request.ai_status,
            )
            uow.decisions.add(decision)
            return decision

        entry_price = request.limit_price or request.entry_price
        sizing_input: SizingInput | None = None
        if self.tracker.equity > 0:
            sizing_input = SizingInput(
                self.tracker.equity,
                request.direction,
                entry_price,
                request.stop_price,
                request.ai_factor,
                market.fee_rate,
                market.slippage,
                market.instrument,
                market.leverage,
                market.mmr,
            )
            original = size_entry(replace(sizing_input, ai_factor=Decimal(1)), self.limits)
            original_qty = original.qty

        episode = uow.episodes.active_for(request.strategy, request.symbol)
        if episode is not None:
            if (
                not request.is_exit
                and episode.direction is request.direction
                and episode.state is EpisodeState.OPEN
            ):
                return Admission(record(DecisionKind.MAINTAINED), None, episode, False)
            rejection = Rejection(RejectionCode.EPISODE_OPEN_OPPOSITE, episode.episode_id)
            return Admission(
                record(DecisionKind.REJECTED, rejections=(rejection,)), None, episode, False
            )

        rejection = request.entry_rejection
        if request.is_exit:
            rejection = Rejection(RejectionCode.EXIT_ONLY, "no active episode")
        elif self.tracker.equity <= 0:
            rejection = Rejection(RejectionCode.PAUSED, "nonpositive equity")
        elif uow.portfolio.paused(request.symbol) or any(
            i.symbol is request.symbol and i.state is not CloseState.DONE
            for i in uow.portfolio.intents()
        ):
            rejection = Rejection(
                RejectionCode.PAUSED, "portfolio reconciliation/close in progress"
            )
        elif any(
            f.status is FillStatus.PENDING_ATTRIBUTION and f.event.symbol is request.symbol
            for f in uow.fills.all()
        ):
            rejection = Rejection(RejectionCode.PAUSED, "unattributed execution")
        elif any(
            q.value * request.direction.sign < 0
            for _, sym, q in uow.positions.all()
            if sym is request.symbol
        ) or any(
            o.symbol is request.symbol
            and (o.reserved_qty + o.unbooked_qty) * request.direction.sign < 0
            for o in uow.orders.all()
        ):
            rejection = Rejection(RejectionCode.POSITION_NOT_FLAT, "opposing strategy exposure")
        elif uow.positions.get(request.strategy, request.symbol).value or any(
            o.strategy is request.strategy
            and o.symbol is request.symbol
            and (o.reserved_qty or o.unbooked_qty)
            for o in uow.orders.all()
        ):
            rejection = Rejection(
                RejectionCode.POSITION_NOT_FLAT, "episode cannot start on existing exposure"
            )
        if rejection:
            return Admission(
                record(DecisionKind.REJECTED, rejections=(rejection,)), None, None, False
            )

        assert sizing_input is not None
        sizing = size_entry(sizing_input, self.limits)
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
        if market.tier_notional_limit is not None:
            symbol_now = sum(
                (row.total_notional for row in view.symbols if row.symbol is request.symbol),
                Decimal(0),
            )
            basis += (("tier_notional_limit", str(market.tier_notional_limit)),)
            if symbol_now + abs(sizing.qty.value) * notional_price > market.tier_notional_limit:
                rejection = Rejection(RejectionCode.TIER_NOTIONAL, "contract tier-1 notional cap")
                return Admission(
                    record(DecisionKind.REJECTED, rejections=(rejection,), basis=basis),
                    None,
                    None,
                    False,
                )
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
            entry_price,
            OrderState.CREATED,
            Qty(Decimal(0)),
            Decimal(0),
            now,
            now,
        )
        assert sizing.aligned_stop is not None
        order = replace(
            order,
            attached_stop=StopParameters(Price(sizing.aligned_stop), request.stop_trigger_by),
            expires_at=request.expires_at,
            post_only=request.post_only,
            qty=entry_delta(
                sizing.qty,
                uow.positions.get(request.strategy, request.symbol),
                Qty(Decimal(0)),
                Qty(Decimal(0)),
            ),
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
