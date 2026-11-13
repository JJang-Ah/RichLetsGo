"""Resumable D76 boundaries and nonblocking protective/control maintenance."""

import asyncio
import copy
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from richletsgo.control.service import CommandService
from richletsgo.domain import Clock, StrategyId, Symbol
from richletsgo.marketdata.models import Bar, Interval
from richletsgo.oms.synchronizer import Synchronizer
from richletsgo.portfolio.funding import FundingSnapshotMissing, allocate, position_snapshot
from richletsgo.portfolio.service import PortfolioService
from richletsgo.portfolio.signals import MarketSignal
from richletsgo.risk.checks import StrategyRiskInput, evaluate_pauses
from richletsgo.risk.equity import AccountSnapshot, CashFlow, EquityTracker
from richletsgo.risk.gate import Admission, MarketInputs, RiskGate
from richletsgo.risk.guards import DATA_PAUSE_REASON
from richletsgo.risk.pause import Pause, PauseKind
from richletsgo.storage.codec import decode, encode
from richletsgo.storage.contracts import UnitOfWork
from richletsgo.storage.errors import CommitOutcomeUnknown
from richletsgo.strategy.boundary import boundary_steps


@dataclass(frozen=True)
class FundingPayment:
    funding_id: str
    symbol: Symbol
    settled_at: datetime
    cost: Decimal


@dataclass(frozen=True)
class LoopPolicy:
    tick: timedelta
    bar_wait: timedelta
    max_signal_age: timedelta
    rest_every: timedelta = timedelta(minutes=1)
    products_every: timedelta = timedelta(days=1)

    def __post_init__(self) -> None:
        if any(
            v <= timedelta(0)
            for v in (
                self.tick,
                self.bar_wait,
                self.max_signal_age,
                self.rest_every,
                self.products_every,
            )
        ):
            raise ValueError("positive Trader loop durations required")


class LiveInputs(Protocol):
    async def route(
        self,
        signal: MarketSignal,
        market: MarketInputs,
        figures: dict[StrategyId, StrategyRiskInput],
    ) -> Admission: ...
    async def check_liquidation(self) -> None: ...
    async def warmup(self) -> tuple[Bar, ...]: ...
    async def bar(self, symbol: Symbol, interval: Interval, at: datetime) -> Bar | None: ...
    async def funding(self, since: datetime) -> tuple[FundingPayment, ...]: ...
    async def account(self) -> AccountSnapshot: ...
    async def cash_flows(self, since: datetime) -> tuple[CashFlow, ...]: ...
    async def maintain(self) -> None: ...
    async def market(self, symbol: Symbol) -> MarketInputs: ...
    async def figures(
        self, uow: UnitOfWork, baseline: dict[str, str]
    ) -> dict[StrategyId, StrategyRiskInput]: ...
    async def signals(
        self, uow: UnitOfWork, at: datetime, bars: tuple[Bar, ...], available: frozenset[Symbol]
    ) -> tuple[MarketSignal, ...]: ...


class BoundaryWaiting(Exception):
    """A boundary may wait; periodic protection and operator control must keep running."""


class Trader:
    def __init__(
        self,
        *,
        uow: Callable[[], UnitOfWork],
        clock: Clock,
        gate: RiskGate,
        portfolio: PortfolioService,
        synchronizer: Synchronizer,
        control: CommandService,
        inputs: LiveInputs,
        policy: LoopPolicy,
        symbols: tuple[Symbol, ...],
        resolve_commit: Callable[[str], Awaitable[bool]],
        recover: Callable[[], Awaitable[None]],
        config_record: str,
        alert: Callable[[str], None],
        initialize_tracker: bool = False,
    ) -> None:
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("unique Trader symbols required")
        self.uow, self.clock, self.gate, self.portfolio, self.sync = (
            uow,
            clock,
            gate,
            portfolio,
            synchronizer,
        )
        self.control, self.inputs, self.policy, self.symbols = control, inputs, policy, symbols
        self.gate.audit_runtime = True
        self.resolve_commit, self.recover, self.alert = resolve_commit, recover, alert
        self.ready = False
        self._boot_complete = False
        self.funding_wait = False
        self.stopping = False
        self.sync.sender.entry_ready = lambda: self.gate.entry_ready()
        self.sync.sender.entry_allowed = lambda order: (
            self.gate.entry_ready() and not self.gate.pauses.blocking(order.strategy, order.symbol)
        )
        self.gate.entry_ready = self.can_enter
        self._bars: dict[tuple[datetime, Symbol, Interval], Bar] = {}
        self._boundaries: set[datetime] = set()
        self._pending_txn: str | None = None
        self._pending_publish: tuple[Any, ...] | None = None
        self._last_tick: datetime | None = None
        self._last_rest: datetime | None = None
        self._last_products: datetime | None = None
        self.last_boundary: datetime | None = None
        self.last_commit: datetime | None = None
        self._lifecycle = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._boundary_lock = asyncio.Lock()
        self.config_record = config_record
        self.initialize_tracker = initialize_tracker

    def can_enter(self) -> bool:
        return (
            self.ready
            and self._boot_complete
            and not self.funding_wait
            and not self.stopping
            and self.sync.gateway.connected
            and self.gate.tracker.day_start == Interval.DAY.floor(self.clock.now())
            and self.gate.tracker.equity > 0
        )

    async def _commit(self, uow: UnitOfWork) -> None:
        try:
            await uow.commit()
        except CommitOutcomeUnknown as error:
            self._pending_txn = error.txn_id
            self.ready = False
            raise
        except OSError:
            self._pending_publish = None
            raise
        self.last_commit = self.clock.now()

    def retain_created(self, u: UnitOfWork) -> None:
        for at in self._boundaries:
            if self.clock.now() - at > self.policy.max_signal_age:
                continue
            output = u.recovery.get("boundary_output:" + at.isoformat())
            if output is None:
                continue
            for signal in decode(output):
                decision = u.decisions.get(signal.signal_id)
                order = (
                    u.orders.get(decision.order_key) if decision and decision.order_key else None
                )
                if (
                    order is not None
                    and order.state.value == "CREATED"
                    and (order.expires_at is None or order.expires_at > self.clock.now())
                ):
                    self.sync.preserve_created_entries.add(order.key)
                    self.sync.sender.proven_entries.add(order.key)

    async def boot(self) -> bool:
        async with self._lifecycle:
            self.ready = False
            self._boot_complete = False
            try:
                await self.control.startup()
                async with self.uow() as u:
                    # Config is public and hashed by the loader; credentials never enter this record.
                    u.recovery.put(
                        "config_version:" + hashlib.sha256(self.config_record.encode()).hexdigest(),
                        self.config_record,
                    )
                    old = u.recovery.get("trader:last_boundary")
                    self.last_boundary = datetime.fromisoformat(old) if old else None
                    for _, raw in u.recovery.active("boundary_input:"):
                        item = decode(raw)
                        if isinstance(item, dict) and "at" in item:
                            at = item["at"]
                            if self.last_boundary is None or at > self.last_boundary:
                                self._boundaries.add(at)
                    self.retain_created(u)
                    await self._commit(u)
                if not await self.sync.synchronize():
                    return (
                        False  # Includes stabilization limit and out-of-range watermark failures.
                    )
                for bar in await self.inputs.warmup():
                    self._bars[bar.end, bar.symbol, bar.interval] = bar
                # Expired entries and protective actions run before opening the memory gate.
                await self.sync.tick()
                await self.control.tick()
                cursor = (
                    self.last_boundary + Interval.HOUR.duration
                    if self.last_boundary
                    else Interval.HOUR.floor(self.clock.now())
                )
                while cursor <= Interval.HOUR.floor(self.clock.now()):
                    self._boundaries.add(cursor)
                    cursor += Interval.HOUR.duration
                self._boot_complete = True
                self.ready = True
                return True
            except (OSError, ValueError):
                self.alert("Trader boot incomplete")
                return False

    async def accept(self, bar: Bar) -> None:
        if bar.symbol not in self.symbols or bar.interval not in (Interval.HOUR, Interval.DAY):
            raise ValueError("unexpected Trader candle")
        if self.last_boundary is not None and bar.end <= self.last_boundary:
            return
        key = (bar.end, bar.symbol, bar.interval)
        previous = self._bars.get(key)
        if previous is not None and not previous.same_values(bar):
            self.ready = False
            raise ValueError("conflicting confirmed candle")
        self._bars[key] = bar
        self._boundaries.add(bar.end)

    async def maintenance(self) -> None:
        async with self._maintenance_lock:
            await self._maintenance()

    async def _maintenance(self) -> None:
        now = self.clock.now()
        if self._last_tick is not None and now - self._last_tick < self.policy.tick:
            return
        self._last_tick = now
        if Interval.DAY.floor(now) > self.gate.tracker.day_start:
            self.funding_wait = True
        expected = (
            self.last_boundary + Interval.HOUR.duration
            if self.last_boundary
            else Interval.HOUR.floor(now)
        )
        if expected <= Interval.HOUR.floor(now):
            self._boundaries.add(expected)
        if self.sync.sender.pending_entries:
            self.ready = False
            for txn, order in tuple(self.sync.sender.pending_entries.items()):
                if await self.resolve_commit(txn):
                    self.sync.sender.proven_entries.add(order.key)
                else:
                    self.sync.sender.pending_entries.pop(txn, None)
                    await self.sync.discard_unsent(order.key)
            await self.recover()
            self.ready = self._boot_complete and not self.stopping
        if self._pending_txn is not None:
            committed = await self.resolve_commit(self._pending_txn)
            if committed:
                async with self.uow() as u:
                    self.retain_created(u)
            await (
                self.recover()
            )  # Journal and exchange reconciliation before any entry continuation.
            if committed and self._pending_publish:
                tracker, pauses, old_pauses, previous = self._pending_publish
                async with self.gate.account_lock:

                    def operator_risk(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
                        return [
                            p for p in items if p["kind"] != PauseKind.ENTRY_PAUSED_SYSTEM.value
                        ]

                    if (
                        operator_risk(self.gate.pauses.to_json()) == operator_risk(old_pauses)
                        and self.gate.tracker.state == previous
                    ):
                        # Reconciliation may add/remove sync/storage pauses. Keep its
                        # current system set rather than resurrecting a cleared pause.
                        pauses.clear(PauseKind.ENTRY_PAUSED_SYSTEM)
                        for pause in self.gate.pauses.active():
                            if pause.kind is PauseKind.ENTRY_PAUSED_SYSTEM:
                                pauses.add(pause)
                        self.gate.tracker.publish(tracker)
                        self.gate.pauses.publish(pauses)
                    else:
                        self.gate.pauses.add(
                            Pause(PauseKind.ENTRY_PAUSED_SYSTEM, "boundary_state_reconcile", now)
                        )
            self._pending_publish = None
            self._pending_txn = None
            self.ready = True
        if self.uow().entry_paused:
            self.ready = False
            await self.recover()
            self.ready = self._boot_complete and not self.stopping
        await self.sync.tick()
        await self.inputs.check_liquidation()
        await self.control.tick()  # Actual terminal order observations, not outbox acknowledgement.
        if self._last_rest is None or now - self._last_rest >= self.policy.rest_every:
            self.ready = False
            stable = await self.sync.synchronize()
            self.ready = stable and self._boot_complete and not self.stopping
            if stable and Interval.DAY.floor(now) == self.gate.tracker.day_start:
                await self.inputs.market(self.symbols[0])
                snapshot = await self.inputs.account()
                flows = await self.inputs.cash_flows(self.gate.tracker.state.latest.at)
                await self.observe(snapshot, flows)
            self._last_rest = now
        if self._last_products is None or now - self._last_products >= self.policy.products_every:
            await self.inputs.maintain()
            self._last_products = now
        for txn in tuple(self.sync.sender.pending_entries):
            order = self.sync.sender.pending_entries[txn]
            if not self.gate.pauses.blocking(order.strategy, order.symbol):
                await self.sync.sender.resume_entry_commit(txn, self.resolve_commit)

    async def step(self) -> None:
        await self.maintenance()
        if not self.ready or self.stopping:
            return
        async with self._boundary_lock:
            if self._boundaries:
                await self.process(min(self._boundaries))

    async def process(self, at: datetime) -> bool:
        root = at.isoformat()
        key = "boundary_input:" + root
        async with self.uow() as u:
            saved = u.recovery.get(key)
        if saved:
            source = decode(saved)
        else:
            intervals = (
                (Interval.HOUR, Interval.DAY) if Interval.DAY.aligned(at) else (Interval.HOUR,)
            )
            bars, available = [], set(self.symbols)
            for symbol in self.symbols:
                for interval in intervals:
                    bar = self._bars.get((at, symbol, interval))
                    if bar is None and self.clock.now() < at + self.policy.bar_wait:
                        return False
                    if bar is None:
                        bar = await self.inputs.bar(symbol, interval, at)
                    if bar is None:
                        available.discard(symbol)
                        self.gate.pauses.add(
                            Pause(
                                PauseKind.ENTRY_PAUSED_SYSTEM,
                                DATA_PAUSE_REASON,
                                self.clock.now(),
                                symbol=symbol,
                            )
                        )
                        self.alert("missing confirmed candle: " + symbol.value)
                    elif bar.end != at or bar.symbol is not symbol or bar.interval is not interval:
                        raise ValueError("REST boundary candle mismatch")
                    else:
                        bars.append(bar)
            source = {
                "at": at,
                "bars": tuple(bars),
                "available": tuple(sorted(available, key=lambda s: s.value)),
            }
            async with self.uow() as u:
                u.recovery.put(key, encode(source))
                await self._commit(u)

        async def done(u: UnitOfWork, stage: str) -> bool:
            return u.recovery.get("boundary_checkpoint:" + root + ":" + stage) is not None

        async def mark(u: UnitOfWork, stage: str) -> None:
            u.recovery.put("boundary_checkpoint:" + root + ":" + stage, "true")
            await self._commit(u)

        async def fills() -> None:
            # Authoritative lookup plus buffered execution ingestion precedes the freeze.
            await self.sync.tick()
            if not await self.sync.confirm_fills(at - self.sync.policy.history_window):
                raise BoundaryWaiting
            async with self.uow() as u:
                if not await done(u, "fills"):
                    for symbol in self.symbols:
                        position_snapshot(u, at, symbol)
                    await mark(u, "fills")

        async def funding() -> None:
            payments = await self.inputs.funding(at - self.sync.policy.history_window)
            async with self.uow() as u:
                for payment in payments:
                    if not u.portfolio.funding_seen(payment.funding_id):
                        try:
                            allocate(
                                u,
                                payment.funding_id,
                                payment.symbol,
                                payment.cost,
                                payment.settled_at,
                            )
                        except FundingSnapshotMissing:
                            u.recovery.put(
                                "pending_funding:" + payment.funding_id,
                                json.dumps(
                                    {
                                        "symbol": payment.symbol.value,
                                        "at": payment.settled_at.isoformat(),
                                        "cost": str(payment.cost),
                                    }
                                ),
                            )
                            continue
                    u.recovery.put(
                        "funding_received:"
                        + payment.settled_at.isoformat()
                        + ":"
                        + payment.symbol.value,
                        "true",
                    )
                waiting = False
                if Interval.DAY.aligned(at):
                    for symbol in self.symbols:
                        positions = position_snapshot(u, at, symbol)
                        if (
                            any(Decimal(q) for q in positions.values())
                            and u.recovery.get("funding_received:" + root + ":" + symbol.value)
                            is None
                        ):
                            waiting = True
                if waiting:
                    await self._commit(u)
                    self.funding_wait = True
                    self.alert("midnight funding pending: entries paused for all symbols")
                    raise BoundaryWaiting
                self.funding_wait = False
                await mark(u, "funding")

        async def account() -> None:
            async with self.uow() as u:
                if await done(u, "account"):
                    return
                prepared = u.recovery.get("boundary_input:" + root + ":account")
                first_account = self.initialize_tracker and u.recovery.get("equity_tracker") is None
            if prepared:
                snapshot, flows = decode(prepared)
            else:
                await self.inputs.market(self.symbols[0])
                snapshot = await self.inputs.account()
                if snapshot.at < at:
                    raise BoundaryWaiting
                flows = await self.inputs.cash_flows(
                    Interval.DAY.floor(snapshot.at)
                    if first_account
                    else self.gate.tracker.day_start
                )
                if any(f.at > snapshot.at for f in flows):
                    raise BoundaryWaiting
                async with self.uow() as u:
                    u.recovery.put("boundary_input:" + root + ":account", encode((snapshot, flows)))
                    await self._commit(u)
            async with self.gate.account_lock:
                tracker, pauses = (
                    EquityTracker(self.gate.tracker.state),
                    copy.deepcopy(self.gate.pauses),
                )
                async with self.uow() as u:
                    initial = self.initialize_tracker and u.recovery.get("equity_tracker") is None
                if initial:
                    # First account baseline is initialized only AFTER funding is known.
                    tracker = EquityTracker.open(snapshot)
                    tracker = EquityTracker(
                        replace(tracker.state, seen_flows=frozenset(f.flow_id for f in flows))
                    )
                elif Interval.DAY.aligned(at) and at > tracker.day_start:
                    tracker.start_day(snapshot, flows)
                    pauses.expire(snapshot.at)
                elif Interval.DAY.floor(snapshot.at) == tracker.day_start:
                    tracker.observe(snapshot, flows)
                async with self.uow() as u:
                    if Interval.DAY.aligned(at) and at > self.gate.tracker.day_start:
                        u.recovery.put(
                            "daily_strategy_baseline",
                            json.dumps(
                                {
                                    s.value: str(
                                        sum(
                                            (
                                                b.net_realized
                                                for b in u.portfolio.books()
                                                if b.strategy is s
                                            ),
                                            Decimal(0),
                                        )
                                    )
                                    for s in StrategyId
                                }
                            ),
                        )
                    baseline = json.loads(u.recovery.get("daily_strategy_baseline") or "{}")
                    figures = await self.inputs.figures(u, baseline)
                    for pause in evaluate_pauses(
                        self.gate.limits, tracker, figures, self.clock.now()
                    ):
                        pauses.add(pause)
                    u.recovery.put("equity_tracker", encode(tracker.state))
                    u.recovery.put("pauses", json.dumps(pauses.to_json()))
                    self._pending_publish = (
                        tracker,
                        pauses,
                        self.gate.pauses.to_json(),
                        self.gate.tracker.state,
                    )
                    await mark(u, "account")
                self._pending_publish = None
                self.gate.tracker.publish(tracker)
                self.gate.pauses.publish(pauses)

        async def signals() -> None:
            async with self.uow() as u:
                output = u.recovery.get("boundary_output:" + root)
                if output is None:
                    generated = (
                        ()
                        if self.clock.now() - at > self.policy.max_signal_age
                        else await self.inputs.signals(
                            u, at, source["bars"], frozenset(source["available"])
                        )
                    )
                    u.recovery.put("boundary_output:" + root, encode(generated))
                    await self._commit(u)
                else:
                    generated = decode(output)
            for signal in generated:
                if self.clock.now() - at > self.policy.max_signal_age:
                    break
                market = await self.inputs.market(signal.symbol)
                snapshot = await self.inputs.account()
                flows = await self.inputs.cash_flows(self.gate.tracker.day_start)
                await self.observe(snapshot, flows)
                admission = await self.inputs.route(signal, market, await self._figures())
                if admission.order is not None and admission.order.state.value == "CREATED":
                    await self.sync.submit(admission.order)
            async with self.uow() as u:
                u.recovery.put("trader:last_boundary", root)
                await mark(u, "signals")
            self.last_boundary = at
            self._boundaries.discard(at)

        try:
            await boundary_steps(fills, funding, account, signals)
            return True
        except CommitOutcomeUnknown as error:
            self._pending_txn = error.txn_id
            self.ready = False
            raise
        except (BoundaryWaiting, FundingSnapshotMissing):
            return False

    async def observe(self, snapshot: AccountSnapshot, flows: tuple[CashFlow, ...]) -> None:
        """Publish observations and resulting risk pauses only after a known commit."""
        async with self.gate.account_lock:
            if snapshot.at < self.gate.tracker.state.latest.at:
                raise BoundaryWaiting
            tracker, pauses = (
                EquityTracker(self.gate.tracker.state),
                copy.deepcopy(self.gate.pauses),
            )
            tracker.observe(snapshot, flows)
            async with self.uow() as u:
                baseline = json.loads(u.recovery.get("daily_strategy_baseline") or "{}")
                figures = await self.inputs.figures(u, baseline)
                pauses.expire(self.clock.now())
                for pause in evaluate_pauses(self.gate.limits, tracker, figures, self.clock.now()):
                    pauses.add(pause)
                u.recovery.put("equity_tracker", encode(tracker.state))
                u.recovery.put("pauses", json.dumps(pauses.to_json()))
                self._pending_publish = (
                    tracker,
                    pauses,
                    self.gate.pauses.to_json(),
                    self.gate.tracker.state,
                )
                await self._commit(u)
            self._pending_publish = None
            self.gate.tracker.publish(tracker)
            self.gate.pauses.publish(pauses)

    async def _figures(self) -> dict[StrategyId, StrategyRiskInput]:
        async with self.uow() as u:
            baseline = json.loads(u.recovery.get("daily_strategy_baseline") or "{}")
            result = await self.inputs.figures(u, baseline)
            await self._commit(u)
            return result

    async def close(self) -> None:
        self.stopping = True
        self.ready = False
        async with self._boundary_lock:
            await self.sync.close()

    def health(self) -> dict[str, Any]:
        return {
            "alive": not self.stopping,
            "ready": self.can_enter(),
            "last_boundary": self.last_boundary.isoformat() if self.last_boundary else None,
            "private_ws": self.sync.gateway.connected,
            "last_commit": self.last_commit.isoformat() if self.last_commit else None,
            "pauses": [p.kind.value for p in self.gate.pauses.active()],
        }
