"""Account-serialized event ingestion, reconciliation and protective outbox runner.

The stream collector only buffers; all repository mutations run under the
coordinator lock. Timers are caller-driven through tick(), never wall-clock sleeps.
"""

import asyncio
import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from richletsgo.domain import (
    Clock,
    ExecutionEvent,
    OrderEvent,
    OrderPurpose,
    OrderState,
    Price,
    Qty,
    Symbol,
    make_order_link_id,
)
from richletsgo.exchange.contracts import (
    Accepted,
    ErrorCode,
    ExchangeError,
    ExchangeGateway,
    PrivateEvent,
)
from richletsgo.portfolio.models import Action, ActionKind, StrategyBook
from richletsgo.portfolio.service import CONFIRMED, STOPS, PortfolioService, invariants, remaining
from richletsgo.portfolio.trailing import (
    can_amend,
    ingest_trail_record,
    recoveries,
    recovery,
    settle_trails,
)
from richletsgo.risk.equity import AccountSnapshot
from richletsgo.risk.pause import PauseKind, PauseState
from richletsgo.risk.records import EpisodeState
from richletsgo.storage.contracts import FillStatus, UnitOfWork

from .journal import JournalCorrupt, JournalRecord, ProtectiveJournal
from .models import OrderRecord
from .recovery import CancelResult, RecoveryAction, resolve_unknown
from .transmission import OrderSender, decode_order


@dataclass(frozen=True, slots=True)
class SyncPolicy:
    history_window: timedelta
    overlap: timedelta
    unknown_alert_after: timedelta
    cancel_retry_after: timedelta
    max_rounds: int

    def __post_init__(self) -> None:
        if (
            self.history_window <= timedelta(0)
            or self.overlap < timedelta(0)
            or self.overlap >= self.history_window
            or self.unknown_alert_after <= timedelta(0)
            or self.cancel_retry_after <= timedelta(0)
            or self.max_rounds < 1
        ):
            raise ValueError("invalid synchronization policy")


class Synchronizer:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        gateway: ExchangeGateway,
        portfolio: PortfolioService,
        journal: ProtectiveJournal,
        clock: Clock,
        pauses: PauseState,
        unique: Callable[[], str],
        policy: SyncPolicy,
        symbols: tuple[Symbol, ...],
    ) -> None:
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("unique account symbols required")
        self.uow_factory, self.gateway, self.portfolio = uow_factory, gateway, portfolio
        self.journal, self.clock, self.pauses, self.policy, self.symbols = (
            journal,
            clock,
            pauses,
            policy,
            symbols,
        )
        self.alerts: list[str] = []
        self.sender = OrderSender(uow_factory, gateway, journal, clock, pauses, unique, self.alerts)
        self._buffer: deque[PrivateEvent] = deque()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._booted = False
        self._generation = 0
        self.account: AccountSnapshot | None = None
        self.sender.pause("sync")

    async def _collect(self) -> None:
        try:
            async for event in self.gateway.private_events():
                self._buffer.append(event)
                self._generation += 1
        finally:
            self.sender.pause("stream")

    async def start_stream(self) -> None:
        if self._task is None or self._task.done():
            if not self.gateway.connected:
                self.sender.pause("stream")
                return
            self._task = asyncio.create_task(self._collect())
            await asyncio.sleep(0)  # scheduler handoff only; no elapsed-time dependency

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _persist_pauses(self) -> None:
        async with self.uow_factory() as uow:
            uow.recovery.put("pauses", json.dumps(self.pauses.to_json()))
            await uow.commit()

    async def _drain(self) -> None:
        await asyncio.sleep(0)
        while self._buffer:
            event = self._buffer[0]
            if isinstance(event, ExecutionEvent):
                await self.portfolio.on_execution(event)
            elif isinstance(event, OrderEvent):
                await self._order(event, confirmed=False)
            self._buffer.popleft()  # keep the event if its transaction failed
        await self.portfolio.tick()

    async def _order(self, event: OrderEvent, *, confirmed: bool) -> None:
        # D65 can discover an emergency request whose CREATED DB record survived.
        async with self.uow_factory() as uow:
            record = uow.orders.find(event.order_link_id, event.exchange_order_id)
            if (
                record is not None
                and record.purpose is not OrderPurpose.ENTRY
                and record.state is OrderState.ABORTED
            ):
                uow.orders.put(replace(record, state=OrderState.UNKNOWN))
                await uow.commit()
        await self.portfolio.on_order(
            event,
            reference_price=event.limit_price or event.trigger_price or Price(Decimal(1)),
            lookup_confirmed=confirmed,
        )

    async def _since(self) -> datetime | None:
        now = self.clock.now()
        starts: list[datetime] = []
        gap = False
        async with self.uow_factory() as uow:
            for symbol in self.symbols:
                key = "reconciliation:" + symbol.value
                raw = uow.recovery.get(key)
                times = [f.event.exec_time for f in uow.fills.all() if f.event.symbol is symbol]
                checked = datetime.fromisoformat(raw) if raw else max(times, default=None)
                gap_key = "history_gap:" + symbol.value
                if uow.recovery.get(gap_key) or (
                    checked is not None
                    and checked < now - self.policy.history_window + self.policy.overlap
                ):
                    uow.recovery.put(gap_key, "unresolved")
                    gap = True
                starts.append(
                    checked - self.policy.overlap if checked else now - self.policy.history_window
                )
            await uow.commit()
        if gap:
            self.sender.pause("history_out_of_range")
            return None
        return min(starts)

    async def _load_journal(self) -> tuple[JournalRecord, ...]:
        records = self.journal.read()
        async with self.uow_factory() as uow:
            saved = uow.recovery.get("pauses")
            if saved and not self._booted:
                for pause in PauseState.from_json(json.loads(saved)).active():
                    self.pauses.add(pause)
            # Include emergency records saved in DB even when fsync failed.
            combined = {
                JournalRecord.decode(v).key: JournalRecord.decode(v)
                for k, v in uow.recovery.items()
                if k.startswith("journal:")
            }
            combined.update({r.key: r for r in records})
            for record in combined.values():
                uow.recovery.put(record.key, record.encode())
                ingest_trail_record(uow, record)
                if record.action_key:
                    uow.portfolio.acknowledge(record.action_key)
                payload = record.payload()
                if record.phase == "requested" and record.op_type in ("create", "close"):
                    restored = decode_order(payload["order"])
                    current = uow.orders.get(restored.key)
                    if current is None:
                        uow.orders.put(restored)
                    elif current.state is OrderState.CREATED:
                        uow.orders.put(replace(current, state=OrderState.UNKNOWN))
            if not self._booted:
                for order in uow.orders.all():
                    if order.state in (OrderState.CREATED, OrderState.SUBMITTED):
                        uow.orders.put(
                            replace(
                                order,
                                state=OrderState.ABORTED
                                if order.state is OrderState.CREATED
                                else OrderState.UNKNOWN,
                            )
                        )
                for episode in uow.episodes.all():
                    entries = [uow.orders.get(k) for k in episode.entry_order_keys]
                    if entries and all(
                        o is not None and o.state is OrderState.ABORTED and not o.booked_qty.value
                        for o in entries
                    ):
                        uow.episodes.put(replace(episode, state=EpisodeState.CLOSED))
            await uow.commit()
        self._booted = True
        return tuple(sorted(combined.values(), key=lambda r: (r.seq, r.operation_id, r.phase)))

    async def _replay_outcomes(self, records: tuple[JournalRecord, ...]) -> None:
        async with self.uow_factory() as uow:
            for record in records:
                if record.phase != "requested":
                    continue
                order = uow.orders.find(record.target, None) or uow.orders.find(None, record.target)
                if order is None:
                    outcome = "UNKNOWN"
                elif record.op_type in ("create", "close"):
                    outcome = (
                        "UNKNOWN"
                        if order.state in (OrderState.UNKNOWN, OrderState.SUBMITTED)
                        else order.state.value
                    )
                elif record.op_type == "cancel":
                    outcome = "TERMINAL" if order.state.is_terminal else "OPEN"
                else:
                    payload = record.payload()
                    current = {
                        "trigger": str(order.trigger_price.value) if order.trigger_price else None,
                        "qty": str(order.qty.value),
                    }

                    matches = {
                        name: all(
                            current[f] == payload[name][f]
                            or (
                                current[f] is not None
                                and payload[name][f] is not None
                                and Decimal(str(current[f])) == Decimal(payload[name][f])
                            )
                            for f in payload["fields"]
                        )
                        for name in ("before", "after")
                    }
                    outcome = (
                        "SUPERSEDED"
                        if order.state.is_terminal
                        else "APPLIED"
                        if matches["after"]
                        else "NOT_APPLIED"
                        if matches["before"]
                        else "SUPERSEDED"
                    )
                uow.recovery.put("outcome:" + record.operation_id, outcome)
            await uow.commit()
        self.journal.archive({r.key for r in records})

    async def _refresh(self, symbol: Symbol, since: datetime) -> tuple[OrderEvent, ...] | None:
        started = self.clock.now()
        history = await self.gateway.get_order_history(symbol, since)
        executions = await self.gateway.get_executions(symbol, since)
        opened = await self.gateway.get_open_orders(symbol)
        if any(isinstance(r, ExchangeError) for r in (history, executions, opened)):
            self.sender.pause("query")
            return None
        assert (
            not isinstance(history, ExchangeError)
            and not isinstance(executions, ExchangeError)
            and not isinstance(opened, ExchangeError)
        )
        # Parents before attached children, then fills; execution delivery itself
        # still exercises pending attribution via the live buffer.
        for event in sorted(
            (*history, *opened), key=lambda e: (bool(e.parent_order_link_id), e.event_time)
        ):
            await self._order(event, confirmed=True)
        for execution in executions:
            await self.portfolio.on_execution(execution)
        async with self.uow_factory() as uow:
            observed = frozenset(
                o.key
                for event in (*history, *opened)
                if (o := uow.orders.find(event.order_link_id, event.exchange_order_id)) is not None
            )
            settle_trails(
                uow,
                self.clock.now(),
                self.portfolio.policy,
                queried=observed,
                query_started=started,
            )
            unresolved = any(
                o.symbol is symbol
                and (
                    o.exchange_cum_exec_qty > abs(o.booked_qty.value)
                    or o.state in (OrderState.SUBMITTED, OrderState.UNKNOWN)
                )
                for o in uow.orders.all()
            ) or any(
                f.event.symbol is symbol and f.status is FillStatus.PENDING_ATTRIBUTION
                for f in uow.fills.all()
            )
            if not unresolved and not uow.recovery.get("history_gap:" + symbol.value):
                uow.recovery.put("reconciliation:" + symbol.value, started.isoformat())
            await uow.commit()
        return opened

    async def _query_stops(self, symbol: Symbol, since: datetime) -> bool:
        await self._drain()
        started = self.clock.now()
        generation = self._generation
        opened = await self._refresh(symbol, since)
        await self._drain()
        if opened is None or self._generation != generation:
            return False
        async with self.uow_factory() as uow:
            last = uow.portfolio.last_fill_at(symbol)
            previous = uow.portfolio.stop_snapshot_at(symbol)
            if last is not None and started <= last:
                return False
            if previous is not None and started <= previous:
                return started == previous
            keys = []
            for event in opened:
                order = uow.orders.find(event.order_link_id, event.exchange_order_id)
                if order and order.purpose in STOPS and order.state in CONFIRMED:
                    keys.append(order.key)
        try:
            await self.portfolio.confirm_stop_snapshot(symbol, keys, started)
        except ValueError:
            return False  # a newer stop/fill invalidated the query
        return True

    async def _audit_unsettled(self) -> None:
        async with self.uow_factory() as uow:
            pending = {
                f.event.symbol
                for f in uow.fills.all()
                if f.status is FillStatus.PENDING_ATTRIBUTION
            }
            delayed: set[Symbol] = set()
            for order in uow.orders.all():
                key = "unbooked:" + order.key
                if order.exchange_cum_exec_qty > abs(order.booked_qty.value):
                    since = uow.recovery.get(key)
                    if not since:
                        since = self.clock.now().isoformat()
                        uow.recovery.put(key, since)
                    if (
                        self.clock.now() - datetime.fromisoformat(since)
                        >= self.policy.unknown_alert_after
                    ):
                        delayed.add(order.symbol)
                else:
                    uow.recovery.put(key, "")
            await uow.commit()
        for symbol in pending:
            self.sender.pause("pending_attribution", symbol)
        for symbol in delayed:
            self.sender.pause("unbooked", symbol)

    async def _unknowns(self) -> None:
        async with self.uow_factory() as uow:
            orders = uow.orders.all()
        for order in orders:
            if order.state is not OrderState.UNKNOWN:
                continue
            self.sender.pause("unknown")
            key = "unknown:" + order.key
            async with self.uow_factory() as uow:
                raw = uow.recovery.get(key)
            data: dict[str, Any] = (
                json.loads(raw)
                if raw
                else {
                    "since": order.updated_at.isoformat(),
                    "alerted": False,
                    "last": None,
                    "cancel": CancelResult.NOT_ATTEMPTED.value,
                }
            )
            decision = resolve_unknown(
                order,
                (),
                CancelResult(data["cancel"]),
                now=self.clock.now(),
                unknown_since=datetime.fromisoformat(data["since"]),
                alert_after=self.policy.unknown_alert_after,
                alerted=data["alerted"],
                last_cancel_at=datetime.fromisoformat(data["last"]) if data["last"] else None,
                cancel_retry_after=self.policy.cancel_retry_after,
            )
            data["alerted"] = decision.alerted
            if decision.action is RecoveryAction.ALERT_MANUAL_CHECK:
                self.alerts.append("manual UNKNOWN check: " + order.key)
            if decision.action is RecoveryAction.SEND_CANCEL:
                data["last"] = self.clock.now().isoformat()
                # Persist actual-attempt time BEFORE external cancellation.
                async with self.uow_factory() as uow:
                    uow.recovery.put(key, json.dumps(data))
                    await uow.commit()
                result = await self.sender.cancel(order)
                data["cancel"] = (
                    CancelResult.ACCEPTED.value
                    if isinstance(result, Accepted)
                    else CancelResult.NOT_FOUND.value
                    if result.code is ErrorCode.NOT_FOUND
                    else CancelResult.TOO_LATE.value
                    if result.code is ErrorCode.TOO_LATE
                    else CancelResult.ERROR.value
                )
            async with self.uow_factory() as uow:
                uow.recovery.put(key, json.dumps(data))
                await uow.commit()

    async def _actions(self, since: datetime) -> None:
        async with self.uow_factory() as uow:
            actions = uow.portfolio.actions()
        for action in actions:
            if action.kind in (ActionKind.ALERT, ActionKind.PAUSE):
                self.alerts.append(action.detail)
            elif action.kind is ActionKind.QUERY_STOPS:
                if not await self._query_stops(action.symbol, since):
                    continue
            else:
                async with self.uow_factory() as uow:
                    order = uow.orders.get(action.order_key or "")
                if order is None:
                    self.sender.pause("missing_action_order")
                    self.alerts.append(
                        "missing action order: manual reconciliation required: " + action.key
                    )
                    continue
                if action.kind in (
                    ActionKind.CLOSE,
                    ActionKind.PROTECT,
                    ActionKind.REDUCE_UNPROTECTED,
                ):
                    await self.sender.send(order, action_key=action.key)
                elif action.kind in (ActionKind.CANCEL_ENTRY, ActionKind.CANCEL_STOP):
                    await self.sender.cancel(order, action_key=action.key)
                elif action.kind is ActionKind.AMEND_STOP:
                    async with self.uow_factory() as uow:
                        current = uow.orders.get(order.key)
                        target = action.trigger
                        attempt = None
                        if current and target and can_amend(uow, current, target):
                            state = recovery(uow, current.key, target)
                            if (
                                action.key == state.action_key
                                and state.attempts < self.portfolio.policy.max_protect_attempts
                                and (
                                    state.last_attempt_at is None
                                    or self.clock.now() - state.last_attempt_at
                                    >= self.portfolio.policy.retry_after
                                )
                            ):
                                attempt = state.attempts
                        if attempt is None:
                            if target is not None:
                                state = recovery(uow, order.key, target)
                                if action.key == state.action_key:
                                    uow.recovery.put(
                                        state.key,
                                        replace(state, discards=state.discards + 1).encode(),
                                    )
                            # Persist the next work identity atomically with removal.
                            uow.portfolio.acknowledge(action.key)
                            await uow.commit()
                            continue
                    if attempt is not None:
                        assert current is not None
                        await self.sender.amend(
                            current, trigger=target, action_key=action.key, trail_attempt=attempt
                        )
                elif action.kind is ActionKind.TRIM_STOP:
                    async with self.uow_factory() as uow:
                        owned = any(
                            order.key in ((state := json.loads(v)).get("target"), state.get("new"))
                            and (not state.get("done") or state.get("exhausted"))
                            for k, v in uow.recovery.items()
                            if k.startswith("replacement:")
                        )
                    if owned:
                        async with self.uow_factory() as uow:
                            uow.portfolio.acknowledge(action.key)
                            await uow.commit()
                        continue
                    # A child stop can arrive before its entry execution. Recheck
                    # excess now: a queued trim must not remove newly needed cover.
                    async with self.uow_factory() as uow:
                        order = uow.orders.get(order.key) or order
                        held = uow.portfolio.book(order.strategy, order.symbol).qty
                        others = sum(
                            (
                                remaining(o)
                                for o in uow.orders.all()
                                if o.key != order.key
                                and o.strategy == order.strategy
                                and o.symbol == order.symbol
                                and o.purpose in STOPS
                                and o.state in CONFIRMED
                                and o.qty.value * held < 0
                            ),
                            Decimal(0),
                        )
                        keep = max(action.qty, abs(held) - others, Decimal(0))
                    if order.state not in CONFIRMED or keep >= remaining(order):
                        async with self.uow_factory() as uow:
                            uow.portfolio.acknowledge(action.key)
                            await uow.commit()
                        continue
                    action = replace(action, qty=keep)
                    if action.qty == 0:
                        await self.sender.cancel(order, action_key=action.key)
                    else:
                        total = order.exchange_cum_exec_qty + action.qty
                        result = await self.sender.amend(
                            order,
                            qty=Qty(total if order.qty.value > 0 else -total),
                            action_key=action.key,
                        )
                        if (
                            isinstance(result, ExchangeError)
                            and result.code is ErrorCode.UNSUPPORTED
                        ):
                            await self._replacement(action, order, since)
            async with self.uow_factory() as uow:
                uow.portfolio.acknowledge(action.key)
                await uow.commit()

    async def _resume_replacements(self, since: datetime) -> None:
        async with self.uow_factory() as uow:
            records = {
                k: JournalRecord.decode(v)
                for k, v in uow.recovery.items()
                if k.startswith("journal:")
            }
        for record in records.values():
            if (
                record.phase != "result"
                or json.loads(record.result).get("code") != ErrorCode.UNSUPPORTED.value
            ):
                continue
            request = records.get(f"journal:{record.operation_id}:requested")
            if request is None or request.op_type != "amend" or not request.action_key:
                continue
            async with self.uow_factory() as uow:
                order = uow.orders.find(request.target, None) or uow.orders.find(
                    None, request.target
                )
            if order is not None:
                payload = request.payload()
                if "qty" not in payload["fields"]:
                    continue
                kept = abs(Decimal(payload["after"]["qty"])) - Decimal(
                    payload["before"]["cumulative"]
                )
                await self._replacement(
                    Action(
                        request.action_key,
                        ActionKind.TRIM_STOP,
                        order.strategy,
                        order.symbol,
                        kept,
                        order.key,
                    ),
                    order,
                    since,
                )

    async def _replacement(self, action: Action, order: OrderRecord, since: datetime) -> None:
        key = "replacement:" + action.key
        async with self.uow_factory() as uow:
            raw = uow.recovery.get(key)
        state: dict[str, Any] = (
            json.loads(raw)
            if raw
            else {"attempt": 0, "last": None, "new": None, "done": False, "target": order.key}
        )
        if state["done"]:
            return
        if not await self._query_stops(order.symbol, since):
            return
        async with self.uow_factory() as uow:
            current = uow.orders.get(order.key)
            books = [b for b in uow.portfolio.books() if b.symbol is order.symbol]
            own = uow.portfolio.book(order.strategy, order.symbol)
            new = uow.orders.get(state["new"]) if state["new"] else None
            # Recover a new request even if its DB progress commit was lost.
            for k, v in uow.recovery.items():
                if k.startswith("journal:"):
                    record = JournalRecord.decode(v)
                    if record.group_id == key and record.step == 3 and record.phase == "requested":
                        if (record.attempt or 0) >= state.get("cancel_attempts", 0):
                            state.update(cancel_attempts=record.attempt or 0, last_cancel=record.ts)
                    if record.group_id == key and record.step == 1 and record.phase == "requested":
                        candidate = decode_order(record.payload()["order"])
                        if (record.attempt or 0) >= state["attempt"]:
                            state.update(
                                attempt=record.attempt or 0, new=candidate.key, last=record.ts
                            )
                            new = uow.orders.get(candidate.key)

        def protected(b: StrategyBook) -> Decimal:
            return sum((stop.qty for stop in b.stops), Decimal(0))

        uncertain_new = new is not None and new.state in (
            OrderState.CREATED,
            OrderState.SUBMITTED,
            OrderState.UNKNOWN,
        )
        if uncertain_new:
            return  # never discard the identity of an unresolved replacement
        if all(protected(b) == abs(b.qty) for b in books):
            state["done"] = True
        elif current is None or current.state.is_terminal:
            state["done"] = True  # normal protection recovery owns any remaining deficit
        else:
            can_cancel = protected(own) - remaining(current) >= abs(own.qty) and all(
                protected(b) >= abs(b.qty) for b in books if b.strategy is not own.strategy
            )
            if can_cancel:
                last_cancel = state.get("last_cancel")
                if (
                    last_cancel
                    and self.clock.now() - datetime.fromisoformat(last_cancel)
                    < self.portfolio.policy.retry_after
                ):
                    return
                if state.get("cancel_attempts", 0) >= self.portfolio.policy.max_close_attempts:
                    self.sender.pause("replacement_exhausted")
                    state.update(done=True, exhausted=True)
                    async with self.uow_factory() as uow:
                        uow.portfolio.pause(order.symbol)
                        uow.recovery.put(key, json.dumps(state))
                        await uow.commit()
                    return
                state["cancel_attempts"] = state.get("cancel_attempts", 0) + 1
                state["last_cancel"] = self.clock.now().isoformat()
                async with self.uow_factory() as uow:
                    uow.recovery.put(key, json.dumps(state))
                    await uow.commit()
                await self.sender.cancel(current, group_id=key, attempt=state["cancel_attempts"])
                # Keep group pending until a fresh whole snapshot verifies cancellation.
                state["last"] = self.clock.now().isoformat()
            elif any(protected(b) < abs(b.qty) for b in books if b.strategy is not own.strategy):
                return  # portfolio recovery repairs other owners before cancellation
            elif new is None or new.state.is_terminal or remaining(new) < action.qty:
                last = datetime.fromisoformat(state["last"]) if state["last"] else None
                if last is not None and self.clock.now() - last < self.portfolio.policy.retry_after:
                    return
                if state["attempt"] >= self.portfolio.policy.max_protect_attempts:
                    self.sender.pause("replacement_exhausted")
                    async with self.uow_factory() as uow:
                        uow.portfolio.pause(order.symbol)
                        await uow.commit()
                    state.update(done=True, exhausted=True)
                else:
                    # Recompute from the current book; never replay an obsolete target.
                    keep = max(Decimal(0), abs(own.qty) - (protected(own) - remaining(current)))
                    if keep == 0:
                        return
                    async with self.uow_factory() as uow:
                        link = make_order_link_id(
                            uow.environment,
                            order.strategy,
                            OrderPurpose.PROTECT,
                            unique_factory=self.sender.unique,
                        )
                        now = self.clock.now()
                        new_order = OrderRecord(
                            link,
                            None,
                            None,
                            order.strategy,
                            OrderPurpose.PROTECT,
                            order.symbol,
                            Qty(-keep if own.qty > 0 else keep),
                            None,
                            current.trigger_price or current.reference_price,
                            OrderState.CREATED,
                            Qty(Decimal(0)),
                            Decimal(0),
                            now,
                            now,
                            trigger_price=current.trigger_price,
                            trigger_by=self.portfolio.policy.trigger_by,
                        )
                        state.update(
                            attempt=state["attempt"] + 1, new=new_order.key, last=now.isoformat()
                        )
                        uow.orders.put(new_order)
                        uow.recovery.put(key, json.dumps(state))
                        await uow.commit()
                    await self.sender.send(
                        new_order, group_id=key, step=1, attempt=state["attempt"]
                    )
        async with self.uow_factory() as uow:
            uow.recovery.put(key, json.dumps(state))
            await uow.commit()

    async def synchronize(self) -> bool:
        async with self._lock:
            self.sender.pause("sync")
            await self.start_stream()
            if not self.gateway.connected:
                return False
            try:
                records = await self._load_journal()
                since = await self._since()
                history_complete = since is not None
                if since is None:
                    since = self.clock.now() - self.policy.history_window
                account = await self.gateway.get_account()
                if isinstance(account, ExchangeError):
                    self.sender.pause("query")
                    return False
                self.account = account
                for _ in range(self.policy.max_rounds):
                    complete = history_complete
                    for symbol in self.symbols:
                        complete = await self._query_stops(symbol, since) and complete
                    await self._audit_unsettled()
                    await self._unknowns()
                    await self._actions(since)
                    await self._resume_replacements(since)
                    await self._drain()
                    if complete:
                        await self._replay_outcomes(records)
                    generation = self._generation
                    positions = await self.gateway.get_positions()
                    await self._drain()
                    if (
                        isinstance(positions, ExchangeError)
                        or self._generation != generation
                        or not complete
                    ):
                        continue
                    async with self.uow_factory() as uow:
                        books = uow.portfolio.books()
                        unsettled = any(
                            o.state
                            in (OrderState.CREATED, OrderState.SUBMITTED, OrderState.UNKNOWN)
                            or o.exchange_cum_exec_qty > abs(o.booked_qty.value)
                            for o in uow.orders.all()
                        )
                        unsettled = unsettled or any(r.in_flight for r in recoveries(uow))
                        pending = any(
                            f.status is FillStatus.PENDING_ATTRIBUTION for f in uow.fills.all()
                        )
                        snapshots = {s: uow.portfolio.stop_snapshot_at(s) for s in self.symbols}
                    if (
                        unsettled
                        or pending
                        or invariants(books, {p.symbol: p.qty for p in positions})
                    ):
                        continue
                    try:
                        for position in positions:
                            at = snapshots.get(position.symbol)
                            if at is not None:
                                await self.portfolio.recover_system_pause(
                                    position.symbol, position.qty, at
                                )
                    except ValueError:
                        continue
                    if not self.gateway.connected or self._buffer:
                        continue
                    for reason in (
                        "sync",
                        "stream",
                        "query",
                        "db",
                        "journal_uncertain",
                        "unknown",
                        "pending_attribution",
                        "unbooked",
                    ):
                        self.pauses.clear(PauseKind.ENTRY_PAUSED_SYSTEM, reason=reason)
                    await self._persist_pauses()
                    return True
                self.sender.pause("sync")
                self.alerts.append("synchronization did not stabilize")
                await self._persist_pauses()
                return False
            except (OSError, JournalCorrupt):
                self.sender.pause("db" if not self.journal.corrupt else "journal_corrupt")
                return False

    async def submit(self, order: OrderRecord) -> Accepted | ExchangeError:
        """Serialize normal submissions with synchronization and account events."""
        async with self._lock:
            result = await self.sender.send(order)
            try:
                await self._drain()
            except OSError:
                self.sender.pause("db")
            return result

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._drain()
                since = await self._since()
                if since is None:
                    since = self.clock.now() - self.policy.history_window
                for symbol in self.symbols:
                    await self._refresh(symbol, since)
                await self._audit_unsettled()
                await self._unknowns()
                await self._actions(since)
                await self._resume_replacements(since)
                if not self.gateway.connected:
                    self.sender.pause("stream")
                await self._persist_pauses()
            except OSError:
                self.sender.pause("db")
