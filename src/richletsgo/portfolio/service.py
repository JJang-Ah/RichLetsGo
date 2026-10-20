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
    StopLink,
    StrategyBook,
    apply_book_fill,
)

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
        if attr.purpose is OrderPurpose.ENTRY and attr.strategy is not None:
            book = uow.portfolio.book(attr.strategy, symbol)
            order = uow.orders.find(event.order_link_id, event.exchange_order_id)
            assert order is not None
            if book.qty * qty < 0:
                raise ValueError("entry cannot reverse an existing strategy position")
            book = apply_book_fill(book, qty, event.price.value, event.fee)
            if order.attached_stop:
                book = replace(book, stop_price=order.attached_stop.stop_loss)
            uow.portfolio.put_book(book)
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
        # Ordinary entry protection. Never assume a locally CREATED/UNKNOWN stop is confirmed.
        for book in uow.portfolio.books():
            if not book.qty:
                continue
            intent = next(
                (
                    i
                    for i in uow.portfolio.intents()
                    if i.symbol is book.symbol and i.state is not CloseState.DONE
                ),
                None,
            )
            if intent and intent.strategy is not book.strategy:
                continue  # close machine owns other-strategy recovery and retry limits
            orders = [
                o
                for o in uow.orders.all()
                if o.symbol is book.symbol and o.strategy is book.strategy and o.purpose in STOPS
            ]
            pending = any(
                o.state in (OrderState.CREATED, OrderState.SUBMITTED, OrderState.UNKNOWN)
                for o in orders
            )
            missing = abs(book.qty) - sum((s.qty for s in book.stops), ZERO)
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
            if missing > 0 and not pending:
                count = sum(o.purpose is OrderPurpose.PROTECT for o in orders)
                self._emit(
                    uow,
                    Action(
                        f"protect:{book.strategy}:{book.symbol}:{count}",
                        ActionKind.PROTECT,
                        book.strategy,
                        book.symbol,
                        -missing if book.qty > 0 else missing,
                    ),
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
        Caller serializes this query with account event ingestion; partial/failed queries
        must never reach this method. A fresh revision gates D58 and final verification.
        """
        require_utc(at)
        async with self.uow_factory() as uow:
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
            uow.portfolio.next_stop_revision(symbol)
            self.reconcile(uow)
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
