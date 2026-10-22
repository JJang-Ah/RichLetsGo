"""Transactional books and protection orchestration. Returned actions are an outbox,
not exchange calls. M1-06/M3 journal and send them, then feed authoritative events back.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from richletsgo.domain import (
    Clock,
    ExecutionEvent,
    InstrumentSpec,
    OrderEvent,
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
from richletsgo.oms.fills import Attribution, FillResult, apply_execution, ingest_order_event
from richletsgo.oms.models import OrderRecord
from richletsgo.risk.records import EpisodeRecord, EpisodeState
from richletsgo.storage.contracts import FillStatus, UnitOfWork

from .closing import CloseView, StrategyView, advance_close
from .models import (
    Action,
    ActionKind,
    CloseIntent,
    ClosePolicy,
    CloseState,
    ProtectionRecovery,
    StopLink,
    StrategyBook,
    apply_book_fill,
)
from .trailing import align_stops, closing_symbol, favorable, recoveries, settle_trails

STOPS = (OrderPurpose.ATTACHED_SL, OrderPurpose.PROTECT)
CONFIRMED = (OrderState.OPEN, OrderState.PARTIALLY_FILLED)
ZERO = Decimal(0)


def remaining(order: OrderRecord) -> Decimal:
    return max(
        ZERO, abs(order.qty.value) - max(abs(order.booked_qty.value), order.exchange_cum_exec_qty)
    )


def settled(uow: UnitOfWork, episode: EpisodeRecord) -> bool:
    entries = [uow.orders.get(k) for k in episode.entry_order_keys]
    if any(o is None or not o.state.is_terminal or o.unbooked_qty for o in entries):
        return False
    # An unattributed fill on the same symbol might belong to this episode.
    return not any(
        f.status is FillStatus.PENDING_ATTRIBUTION and f.event.symbol is episode.symbol
        for f in uow.fills.all()
    )


def invariants(
    books: Sequence[StrategyBook],
    exchange: Mapping[Symbol, Qty],
    *,
    closing: frozenset[Symbol] = frozenset(),
) -> tuple[str, ...]:
    errors: list[str] = []
    for symbol in Symbol:
        rows = [b for b in books if b.symbol is symbol]
        held = sum((b.qty for b in rows), ZERO)
        if symbol in exchange and held != exchange[symbol].value:
            errors.append(f"{symbol}: book/exchange mismatch")
        if symbol in closing:
            continue
        if sum((abs(b.qty) for b in rows), ZERO) != abs(held):
            errors.append(f"{symbol}: opposing strategy positions")
        for b in rows:
            if sum((s.qty for s in b.stops), ZERO) != abs(b.qty):
                errors.append(f"{symbol}/{b.strategy}: protection mismatch")
    return tuple(errors)


class PortfolioService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        unique_factory: Callable[[], str],
        policy: ClosePolicy,
    ) -> None:
        self.uow_factory, self.clock, self.unique, self.policy = (
            uow_factory,
            clock,
            unique_factory,
            policy,
        )

    def trail_stop(
        self,
        uow: UnitOfWork,
        strategy: StrategyId,
        symbol: Symbol,
        spec: InstrumentSpec,
        desired: Price,
    ) -> None:
        """Update the account target only; reconciliation owns order-level work."""
        if not isinstance(spec, InstrumentSpec) or spec.symbol is not symbol:
            raise ValueError("instrument must match target symbol")
        if not isinstance(desired, Price):
            raise TypeError("desired must be Price")
        episode = uow.episodes.active_for(strategy, symbol)
        book = uow.portfolio.book(strategy, symbol)
        if (
            episode is None
            or episode.state is not EpisodeState.OPEN
            or not book.qty
            or closing_symbol(uow, symbol)
        ):
            return
        aligned = (
            spec.round_price_down(desired.value)
            if book.qty > 0
            else spec.round_price_up(desired.value)
        )
        if book.stop_price is None or favorable(aligned, book.stop_price.value, book.qty):
            uow.portfolio.put_book(
                replace(book, stop_price=Price(aligned), stop_episode_id=episode.episode_id)
            )

    def request_close(self, uow: UnitOfWork, episode: EpisodeRecord) -> CloseIntent:
        existing = uow.portfolio.intent(episode.episode_id)
        if existing is not None:
            return existing
        intent = CloseIntent(
            f"close-{episode.episode_id}",
            episode.episode_id,
            episode.strategy,
            episode.symbol,
            self.clock.now(),
        )
        uow.portfolio.put_intent(intent)
        uow.episodes.put(replace(episode, state=EpisodeState.EXITING))
        for key in episode.entry_order_keys:
            order = uow.orders.get(key)
            if order and not order.state.is_terminal:
                self._emit(
                    uow,
                    Action(
                        f"{intent.intent_id}:cancel:{key}",
                        ActionKind.CANCEL_ENTRY,
                        episode.strategy,
                        episode.symbol,
                        order_key=key,
                    ),
                )
        return intent

    def book_execution(self, uow: UnitOfWork, event: ExecutionEvent, attr: Attribution) -> None:
        qty, symbol = event.qty.value, event.symbol
        uow.portfolio.record_fill_at(symbol, self.clock.now())
        if attr.purpose is OrderPurpose.ENTRY and attr.strategy is not None:
            book = uow.portfolio.book(attr.strategy, symbol)
            order = uow.orders.find(event.order_link_id, event.exchange_order_id)
            assert order is not None
            if book.qty * qty < 0:
                raise ValueError("entry cannot reverse an existing strategy position")
            book = apply_book_fill(book, qty, event.price.value, event.fee)
            if order.attached_stop:
                episode = next(
                    (e for e in uow.episodes.all() if order.key in e.entry_order_keys), None
                )
                episode_id = episode.episode_id if episode else None
                target = order.attached_stop.stop_loss
                if (
                    episode_id is not None
                    and episode_id == book.stop_episode_id
                    and book.stop_price is not None
                    and favorable(book.stop_price.value, target.value, qty)
                ):
                    target = book.stop_price
                book = replace(book, stop_price=target, stop_episode_id=episode_id)
            uow.portfolio.put_book(book)
            recovery = uow.portfolio.recovery(attr.strategy, symbol)
            uow.portfolio.put_recovery(
                attr.strategy,
                symbol,
                replace(
                    recovery,
                    snapshot_revision=uow.portfolio.stop_revision(symbol),
                    observed_at=self.clock.now(),
                )
                if recovery
                else ProtectionRecovery(
                    self.unique(), uow.portfolio.stop_revision(symbol), self.clock.now()
                ),
            )
        else:
            strategies = ([attr.strategy] if attr.strategy else []) + [StrategyId.S2, StrategyId.S1]
            seen: set[StrategyId] = set()
            left = abs(qty)
            fee_left = event.fee
            allocations: list[tuple[StrategyBook, Decimal]] = []
            for strategy in strategies:
                if strategy in seen:
                    continue
                seen.add(strategy)
                book = uow.portfolio.book(strategy, symbol)
                amount = min(left, abs(book.qty)) if book.qty * qty < 0 else ZERO
                if amount:
                    allocations.append((book, amount))
                    left -= amount
            if left:
                raise ValueError(
                    "reduction exceeds reconciled holdings; synchronize before booking"
                )
            for index, (book, amount) in enumerate(allocations):
                fee = fee_left if index == len(allocations) - 1 else event.fee * amount / abs(qty)
                fee_left -= fee
                reduced = apply_book_fill(
                    book, amount if qty > 0 else -amount, event.price.value, fee
                )
                uow.portfolio.put_book(reduced)
                episode = uow.episodes.active_for(book.strategy, symbol)
                if episode:
                    self.request_close(uow, episode)
                if attr.external or book.strategy is not attr.strategy:
                    self._emit(
                        uow,
                        Action(
                            f"{event.exec_id}:trim:{book.strategy}",
                            ActionKind.TRIM_STOP,
                            book.strategy,
                            symbol,
                            abs(reduced.qty),
                        ),
                    )
            if attr.external or any(b.strategy is not attr.strategy for b, _ in allocations):
                strategy = attr.strategy or StrategyId.S2
                self._emit(
                    uow,
                    Action(
                        f"{event.exec_id}:alert",
                        ActionKind.ALERT,
                        strategy,
                        symbol,
                        detail="external/overflow reduction allocated S2 then S1",
                    ),
                )
                if attr.external:
                    self._emit(
                        uow,
                        Action(
                            f"{event.exec_id}:pause",
                            ActionKind.PAUSE,
                            strategy,
                            symbol,
                            detail="external reduction",
                        ),
                    )
        self.reconcile(uow)

    def _sync_links(self, uow: UnitOfWork) -> None:
        for book in uow.portfolio.books():
            links = tuple(
                StopLink(o.key, remaining(o))
                for o in uow.orders.all()
                if o.strategy is book.strategy
                and o.symbol is book.symbol
                and o.purpose in STOPS
                and o.state in CONFIRMED
                and o.trigger_price is not None
                and (book.qty == 0 or o.qty.value * book.qty < 0)
                and remaining(o)
            )
            uow.portfolio.put_book(replace(book, stops=links))

    def _view(self, uow: UnitOfWork, episode: EpisodeRecord) -> CloseView:
        views = []
        for strategy in StrategyId:
            book = uow.portfolio.book(strategy, episode.symbol)
            orders = [
                o for o in uow.orders.all() if o.strategy is strategy and o.symbol is episode.symbol
            ]
            outstanding = sum(
                (
                    max(ZERO, o.exchange_cum_exec_qty - abs(o.booked_qty.value))
                    + (remaining(o) if not o.state.is_terminal else ZERO)
                    for o in orders
                    if o.purpose is OrderPurpose.CLOSE
                ),
                ZERO,
            )
            stops = tuple(o.key for o in orders if o.purpose in STOPS and not o.state.is_terminal)
            pending = any(
                o.purpose in STOPS
                and o.state in (OrderState.CREATED, OrderState.SUBMITTED, OrderState.UNKNOWN)
                for o in orders
            )
            views.append(
                StrategyView(
                    strategy,
                    book.qty,
                    sum((s.qty for s in book.stops), ZERO),
                    pending,
                    outstanding,
                    stops,
                )
            )
        entries = tuple(
            k
            for k in episode.entry_order_keys
            if (o := uow.orders.get(k)) is not None and not o.state.is_terminal
        )
        unbooked = any(
            o.symbol is episode.symbol and o.exchange_cum_exec_qty > abs(o.booked_qty.value)
            for o in uow.orders.all()
        )
        pending = any(
            f.event.symbol is episode.symbol and f.status is FillStatus.PENDING_ATTRIBUTION
            for f in uow.fills.all()
        )
        return CloseView(
            tuple(views),
            entries,
            settled(uow, episode),
            pending or unbooked,
            uow.portfolio.stop_revision(episode.symbol),
        )

    def _emit(self, uow: UnitOfWork, action: Action) -> None:
        if uow.portfolio.has_action(action.key):
            return
        if action.kind is ActionKind.TRIM_STOP and action.order_key is None:
            keep = action.qty
            for stop in uow.orders.all():
                if (
                    stop.symbol is not action.symbol
                    or stop.strategy is not action.strategy
                    or stop.purpose not in STOPS
                    or stop.state not in CONFIRMED
                ):
                    continue
                retained = min(keep, remaining(stop))
                keep -= retained
                if retained < remaining(stop):
                    uow.portfolio.emit(
                        replace(
                            action, key=f"{action.key}:{stop.key}", order_key=stop.key, qty=retained
                        )
                    )
            uow.portfolio.emit(replace(action, kind=ActionKind.QUERY_STOPS))
            return
        if action.kind is ActionKind.PAUSE:
            uow.portfolio.pause(action.symbol)
        if action.kind in (ActionKind.CLOSE, ActionKind.REDUCE_UNPROTECTED, ActionKind.PROTECT):
            book = uow.portfolio.book(action.strategy, action.symbol)
            purpose = (
                OrderPurpose.PROTECT if action.kind is ActionKind.PROTECT else OrderPurpose.CLOSE
            )
            if purpose is OrderPurpose.PROTECT and book.stop_price is None:
                uow.portfolio.pause(action.symbol)
                uow.portfolio.emit(
                    replace(
                        action,
                        kind=ActionKind.ALERT,
                        detail="missing stop price; reconciliation required",
                    )
                )
                return
            link = make_order_link_id(
                uow.environment, action.strategy, purpose, unique_factory=self.unique
            )
            reference = book.stop_price or Price(book.avg_price or Decimal(1))
            order = OrderRecord(
                link,
                None,
                None,
                action.strategy,
                purpose,
                action.symbol,
                Qty(action.qty),
                None,
                reference,
                OrderState.CREATED,
                Qty(ZERO),
                ZERO,
                self.clock.now(),
                self.clock.now(),
                trigger_price=book.stop_price if purpose is OrderPurpose.PROTECT else None,
                trigger_by=self.policy.trigger_by if purpose is OrderPurpose.PROTECT else None,
            )
            uow.orders.put(order)
            action = replace(action, order_key=order.key)
        uow.portfolio.emit(action)

    def reconcile(self, uow: UnitOfWork) -> None:
        self._sync_links(uow)
        for episode in uow.episodes.all():
            intent = uow.portfolio.intent(episode.episode_id)
            book = uow.portfolio.book(episode.strategy, episode.symbol)
            if episode.state is EpisodeState.OPEN and book.qty == 0 and settled(uow, episode):
                uow.episodes.put(replace(episode, state=EpisodeState.CLOSED))
            if intent and intent.state is not CloseState.DONE:
                updated, actions = advance_close(
                    intent, self._view(uow, episode), self.clock.now(), self.policy
                )
                uow.portfolio.put_intent(updated)
                for action in actions:
                    self._emit(uow, action)
                if updated.state is CloseState.DONE:
                    uow.episodes.put(replace(episode, state=EpisodeState.CLOSED))
        self._recover_protection(uow)
        settle_trails(uow, self.clock.now(), self.policy)
        align_stops(uow, self.clock.now(), self.policy)

    def _recover_protection(self, uow: UnitOfWork) -> None:
        for book in uow.portfolio.books():
            orders = [
                o
                for o in uow.orders.all()
                if o.symbol is book.symbol and o.strategy is book.strategy
            ]
            pending = any(
                o.purpose in STOPS
                and o.state in (OrderState.CREATED, OrderState.SUBMITTED, OrderState.UNKNOWN)
                for o in orders
            )
            missing = abs(book.qty) - sum((s.qty for s in book.stops), ZERO)
            recovery = uow.portfolio.recovery(book.strategy, book.symbol)
            if missing == 0 and not pending:
                uow.portfolio.put_recovery(book.strategy, book.symbol, None)
                continue
            if not book.qty:
                continue
            if any(
                i.symbol is book.symbol
                and i.strategy is not book.strategy
                and i.state is not CloseState.DONE
                for i in uow.portfolio.intents()
            ):
                continue  # close machine owns other-strategy protection until DONE
            if missing < 0 and not pending:
                self._emit(
                    uow,
                    Action(
                        f"trim:{book.strategy}:{book.symbol}:{uow.portfolio.stop_revision(book.symbol)}:{book.qty}",
                        ActionKind.TRIM_STOP,
                        book.strategy,
                        book.symbol,
                        abs(book.qty),
                    ),
                )
                continue
            if missing <= 0:
                continue
            if recovery is None:
                recovery = ProtectionRecovery(
                    self.unique(), uow.portfolio.stop_revision(book.symbol), self.clock.now()
                )
                uow.portfolio.put_recovery(book.strategy, book.symbol, recovery)
            key = f"protect:{book.strategy}:{book.symbol}:{recovery.recovery_id}"
            self._emit(
                uow,
                Action(
                    f"{key}:query:{uow.portfolio.stop_revision(book.symbol)}",
                    ActionKind.QUERY_STOPS,
                    book.strategy,
                    book.symbol,
                ),
            )
            elapsed = (
                self.clock.now() - recovery.last_attempt_at
                if recovery.last_attempt_at is not None
                else None
            )
            if elapsed is not None and elapsed < self.policy.retry_after:
                continue
            exhausted = recovery.attempts >= self.policy.max_protect_attempts
            timed_out = (
                pending
                and elapsed is not None
                and elapsed >= (self.policy.retry_after * self.policy.max_protect_attempts)
            )
            if exhausted or timed_out or recovery.reduction_attempts:
                self._emit(
                    uow,
                    Action(
                        f"{key}:pause",
                        ActionKind.PAUSE,
                        book.strategy,
                        book.symbol,
                        detail="protection recovery exhausted",
                    ),
                )
                self._emit(
                    uow,
                    Action(
                        f"{key}:alert",
                        ActionKind.ALERT,
                        book.strategy,
                        book.symbol,
                        detail="protection recovery exhausted",
                    ),
                )
                outstanding = sum(
                    (
                        max(ZERO, o.exchange_cum_exec_qty - abs(o.booked_qty.value))
                        + (remaining(o) if not o.state.is_terminal else ZERO)
                        for o in orders
                        if o.purpose is OrderPurpose.CLOSE
                    ),
                    ZERO,
                )
                deficit = max(ZERO, missing - outstanding)
                if not deficit:
                    continue
                if (
                    recovery.last_reduction_at is not None
                    and self.clock.now() - recovery.last_reduction_at < self.policy.retry_after
                ):
                    continue
                if recovery.reduction_attempts >= self.policy.max_close_attempts:
                    self._emit(
                        uow,
                        Action(
                            f"{key}:reduce-exhausted",
                            ActionKind.ALERT,
                            book.strategy,
                            book.symbol,
                            detail="unprotected reduction exhausted; contract remains paused; intervention required",
                        ),
                    )
                else:
                    self._emit(
                        uow,
                        Action(
                            f"{key}:reduce:{recovery.reduction_attempts}",
                            ActionKind.REDUCE_UNPROTECTED,
                            book.strategy,
                            book.symbol,
                            -deficit if book.qty > 0 else deficit,
                        ),
                    )
                    uow.portfolio.put_recovery(
                        book.strategy,
                        book.symbol,
                        replace(
                            recovery,
                            reduction_attempts=recovery.reduction_attempts + 1,
                            last_reduction_at=self.clock.now(),
                        ),
                    )
                continue
            if pending:
                continue
            snapshot_at = uow.portfolio.stop_snapshot_at(book.symbol)
            if (
                uow.portfolio.stop_revision(book.symbol) <= recovery.snapshot_revision
                or snapshot_at is None
                or snapshot_at <= recovery.observed_at
            ):
                continue
            self._emit(
                uow,
                Action(
                    f"{key}:attempt:{recovery.attempts}",
                    ActionKind.PROTECT,
                    book.strategy,
                    book.symbol,
                    -missing if book.qty > 0 else missing,
                ),
            )
            uow.portfolio.put_recovery(
                book.strategy,
                book.symbol,
                replace(recovery, attempts=recovery.attempts + 1, last_attempt_at=self.clock.now()),
            )

    async def on_execution(self, event: ExecutionEvent) -> FillResult:
        return await apply_execution(self.uow_factory(), event, self.book_execution)

    async def on_order(
        self, event: OrderEvent, *, reference_price: Price, lookup_confirmed: bool = False
    ) -> None:
        await ingest_order_event(
            self.uow_factory(),
            event,
            self.book_execution,
            reference_price=reference_price,
            lookup_confirmed=lookup_confirmed,
        )
        await self.tick()

    async def tick(self) -> None:
        async with self.uow_factory() as uow:
            self.reconcile(uow)
            await uow.commit()

    async def confirm_stop_snapshot(
        self, symbol: Symbol, keys: Sequence[str], at: datetime
    ) -> None:
        """After feeding a COMPLETE successful stop query via on_order, confirm its keys.
        at is the query START time. Caller serializes the query with account
        event ingestion; partial/failed queries must never reach this method. A fresh revision gates D58 and final verification.
        """
        require_utc(at)
        async with self.uow_factory() as uow:
            last_fill = uow.portfolio.last_fill_at(symbol)
            if last_fill is not None and at <= last_fill:
                raise ValueError("snapshot must start after the latest booked fill")
            for key in keys:
                o = uow.orders.get(key)
                if (
                    o is None
                    or o.symbol is not symbol
                    or o.purpose not in STOPS
                    or o.state not in CONFIRMED
                ):
                    raise ValueError("snapshot must contain known confirmed stops only")
            for o in uow.orders.all():
                if o.symbol is symbol and o.purpose in STOPS and o.updated_at > at:
                    raise ValueError("snapshot predates a known stop update")
                if (
                    o.symbol is symbol
                    and o.purpose in STOPS
                    and o.key not in keys
                    and o.state in CONFIRMED
                ):
                    uow.orders.put(replace(o, state=OrderState.CANCELLED, updated_at=at))
            previous = uow.portfolio.stop_snapshot_at(symbol)
            if previous is not None and at <= previous:
                raise ValueError("snapshot must advance observation time")
            uow.portfolio.next_stop_revision(symbol, at)
            self.reconcile(uow)
            await uow.commit()

    async def recover_system_pause(self, symbol: Symbol, exchange_qty: Qty, at: datetime) -> None:
        """M1-06 calls after a serialized complete account synchronization at `at`.

        Position and stop snapshots must describe the same synchronized state. This
        clears only the portfolio contract pause, never manual/risk HALTED state.
        """
        require_utc(at)
        async with self.uow_factory() as uow:
            if not uow.portfolio.paused(symbol):
                return
            if (
                uow.portfolio.stop_revision(symbol) <= uow.portfolio.pause_revision(symbol)
                or uow.portfolio.stop_snapshot_at(symbol) != at
            ):
                raise ValueError("fresh synchronized stop snapshot required")
            last_fill = uow.portfolio.last_fill_at(symbol)
            if last_fill is not None and at <= last_fill:
                raise ValueError("snapshot predates a booked fill")
            orders = [o for o in uow.orders.all() if o.symbol is symbol]
            if any(
                o.updated_at > at
                or o.exchange_cum_exec_qty > abs(o.booked_qty.value)
                or (not o.state.is_terminal and o.purpose not in STOPS)
                or o.state in (OrderState.CREATED, OrderState.SUBMITTED, OrderState.UNKNOWN)
                for o in orders
            ):
                raise ValueError("unsettled orders prevent recovery")
            if any(
                f.event.symbol is symbol and f.status is FillStatus.PENDING_ATTRIBUTION
                for f in uow.fills.all()
            ):
                raise ValueError("pending attribution prevents recovery")
            if any(
                i.symbol is symbol and i.state is not CloseState.DONE
                for i in uow.portfolio.intents()
            ):
                raise ValueError("unfinished close prevents recovery")
            if any(
                (r := uow.portfolio.recovery(strategy, symbol)) is not None and r.observed_at >= at
                for strategy in StrategyId
            ):
                raise ValueError("snapshot predates a booked fill")
            for trail in recoveries(uow):
                order = uow.orders.get(trail.order_key)
                if order is None or order.symbol is not symbol or order.state.is_terminal:
                    continue
                book = uow.portfolio.book(order.strategy, symbol)
                if trail.in_flight or (
                    trail.attempts >= self.policy.max_protect_attempts
                    and book.qty
                    and book.stop_price == trail.target
                    and order.trigger_price is not None
                    and favorable(trail.target.value, order.trigger_price.value, book.qty)
                ):
                    raise ValueError("unresolved trailing stop prevents recovery")
            self._sync_links(uow)
            books = [b for b in uow.portfolio.books() if b.symbol is symbol]
            if invariants(books, {symbol: exchange_qty}):
                raise ValueError("position/protection mismatch prevents recovery")
            linked = {s.order_key for b in books for s in b.stops}
            if any(
                o.purpose in STOPS and o.state in CONFIRMED and o.key not in linked for o in orders
            ):
                raise ValueError("unattributed stops prevent recovery")
            if any(
                a.symbol is symbol
                and a.kind not in (ActionKind.QUERY_STOPS, ActionKind.ALERT, ActionKind.PAUSE)
                for a in uow.portfolio.actions()
            ):
                raise ValueError("undelivered actions prevent recovery")
            uow.portfolio.clear_pause(symbol)
            for strategy in StrategyId:
                uow.portfolio.put_recovery(strategy, symbol, None)
            uow.portfolio.emit(
                Action(
                    f"recovered:{symbol}:{uow.portfolio.stop_revision(symbol)}",
                    ActionKind.ALERT,
                    StrategyId.S1,
                    symbol,
                    detail=f"system contract pause cleared after synchronization at {at.isoformat()}",
                )
            )
            await uow.commit()

    async def funding(self, funding_id: str, symbol: Symbol, amount: Decimal) -> None:
        """Allocate exchange funding cost by held absolute quantity, once per event ID."""
        finite_decimal(amount, "funding amount")
        if not funding_id:
            raise ValueError("funding_id required")
        async with self.uow_factory() as uow:
            if uow.portfolio.funding_seen(funding_id):
                return
            books = [b for b in uow.portfolio.books() if b.symbol is symbol and b.qty]
            total = sum((abs(b.qty) for b in books), ZERO)
            if not total and amount:
                raise ValueError("funding needs a reconciled position")
            left = amount
            for index, book in enumerate(books):
                share = left if index == len(books) - 1 else amount * abs(book.qty) / total
                left -= share
                uow.portfolio.put_book(replace(book, funding=book.funding + share))
            uow.portfolio.mark_funding(funding_id)
            await uow.commit()
