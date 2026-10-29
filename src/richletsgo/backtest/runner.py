"""D76 boundary runner. All fills go through SimulatedExchange and the real OMS."""

import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from typing import Any

from richletsgo.domain import Environment, ManualClock, Price, Qty, StrategyId, Symbol
from richletsgo.domain.clock import require_utc
from richletsgo.exchange.contracts import ExchangeError
from richletsgo.exchange.simulated import SimulatedExchange
from richletsgo.marketdata.models import Interval
from richletsgo.marketdata.store import MarketDataStore
from richletsgo.oms.journal import ProtectiveJournal
from richletsgo.oms.synchronizer import Synchronizer, SyncPolicy
from richletsgo.portfolio.models import ClosePolicy
from richletsgo.portfolio.service import PortfolioService
from richletsgo.portfolio.signals import AiMode, AiScoreStore, MarketSignal, SignalRouter
from richletsgo.risk.checks import StrategyRiskInput
from richletsgo.risk.equity import AccountSnapshot, EquityTracker
from richletsgo.risk.gate import MarketInputs, RiskGate
from richletsgo.risk.pause import Pause, PauseKind, PauseState
from richletsgo.risk.records import Direction, EpisodeState
from richletsgo.storage.memory import InMemoryDatabase
from richletsgo.strategy import candidates
from richletsgo.strategy.history import ClosedBars, four_hour_bars
from richletsgo.strategy.models import (
    S1AParams,
    S1BParams,
    S1State,
    S2AParams,
    S2BParams,
    S2Episode,
    S2PEpisode,
    S2PParams,
    S2PState,
    Trend,
)

from .codec import digest, primitive
from .config import ResearchProfile
from .data import RunData, load_data
from .results import BacktestResult, TradeLedger

ZERO = Decimal(0)
HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class BacktestRun:
    start: datetime
    end: datetime
    s1: S1AParams | S1BParams
    s2: S2AParams | S2BParams | S2PParams | None
    profile: ResearchProfile
    close_policy: ClosePolicy
    sync_policy: SyncPolicy
    symbols: tuple[Symbol, ...]
    executed_at: datetime
    code_commit: str

    def __post_init__(self) -> None:
        for at in (self.start, self.end, self.executed_at):
            require_utc(at)
        if (
            not Interval.HOUR.aligned(self.start)
            or not Interval.HOUR.aligned(self.end)
            or self.end <= self.start
        ):
            raise ValueError("nonempty hour-aligned [start,end) required")
        if (
            not self.symbols
            or len(set(self.symbols)) != len(self.symbols)
            or any(not isinstance(s, Symbol) for s in self.symbols)
        ):
            raise ValueError("unique nonempty symbols required")
        object.__setattr__(self, "symbols", tuple(sorted(self.symbols, key=lambda s: s.value)))
        if not isinstance(self.s1, (S1AParams, S1BParams)) or (
            self.s2 is not None and not isinstance(self.s2, (S2AParams, S2BParams, S2PParams))
        ):
            raise TypeError("candidate parameter types required")
        if not self.code_commit:
            raise ValueError("code_commit is required provenance")
        if self.close_policy.trigger_by != "LastPrice":
            raise ValueError("OHLC backtest models LastPrice stops only")

    async def execute(
        self,
        store: MarketDataStore,
        *,
        results_dir: Path,
        journal_dir: Path,
        storage_policy: str = "FULL",
        input_key: str | None = None,
    ) -> BacktestResult:
        if storage_policy not in ("FULL", "SUMMARY"):
            raise ValueError("unknown storage policy")
        data = load_data(store, self.start, self.end, self.symbols, self.s1, self.s2)
        config = primitive(self)
        config.pop("executed_at")
        metadata = {
            "config": config,
            "config_hash": digest(config),
            "data_hash": data.data_hash,
            "code_commit": self.code_commit,
            "executed_at": self.executed_at,
            "tiers": primitive(data.tiers),
            "instruments": primitive(data.instruments),
            "tier_note": "tier-1 snapshot at capture time, not historical margin tiers",
            "ai_mode": "disabled",
        }
        if data.issues:
            result = BacktestResult(
                "INVALID_DATA", data.issues, (), (), (), (), (), (), (), metadata, ()
            )
        else:
            journal_dir.mkdir(parents=True, exist_ok=True)
            # Isolate each execution's journal; never replay a previous run into fresh accounts.
            with tempfile.TemporaryDirectory(prefix="run-", dir=journal_dir) as directory:
                engine = _Engine(self, data, Path(directory))
                result = await engine.execute(metadata)
        if storage_policy == "FULL":
            result.save(results_dir)
        else:
            from .campaign import atomic_json
            from .statistics import summarize

            atomic_json(
                results_dir / (result.result_hash + ".summary.json"),
                summarize(result, input_key or result.metadata["config_hash"]),
            )
        return result


class _Engine:
    def __init__(self, run: BacktestRun, data: RunData, journal_dir: Path) -> None:
        self.run, self.data = run, data
        self.clock = ManualClock(run.start)
        self.db = InMemoryDatabase("research", Environment.BACKTEST)
        identifiers = count(1)
        self.unique = lambda: str(next(identifiers))
        self.pauses = PauseState()
        self.venue = SimulatedExchange(
            instruments=[data.instruments[s].to_spec() for s in run.symbols],
            clock=self.clock,
            wallet_balance=run.profile.start_equity,
            maker_fee=run.profile.maker_fee,
            taker_fee=run.profile.taker_fee,
            slippage=self._slippage,
        )
        self.portfolio = PortfolioService(
            self.db.unit_of_work, self.clock, self.unique, run.close_policy
        )
        self.tracker = EquityTracker.open(
            AccountSnapshot(run.start, run.profile.start_equity, ZERO)
        )
        self.gate = RiskGate(
            account_id="research",
            environment=Environment.BACKTEST,
            uow_factory=self.db.unit_of_work,
            limits=run.profile.limits,
            tracker=self.tracker,
            pauses=self.pauses,
            clock=self.clock,
            unique_factory=self.unique,
        )
        self.router = SignalRouter(
            self.gate,
            self.portfolio,
            AiScoreStore(),
            mode=AiMode.DISABLED,
            threshold=ZERO,
            factor=lambda _: Decimal(1),
            trigger_by=run.close_policy.trigger_by,
        )
        self.sync = Synchronizer(
            self.db.unit_of_work,
            self.venue,
            self.portfolio,
            ProtectiveJournal(journal_dir),
            self.clock,
            self.pauses,
            self.unique,
            run.sync_policy,
            run.symbols,
        )
        self.ledger = TradeLedger()
        self.states = {s: S1State(s, Trend.FLAT, None, None) for s in run.symbols}
        self.marks: dict[Symbol, Price] = {}
        self.daily_base = {s: ZERO for s in StrategyId}
        self.equity: list[dict[str, Any]] = []
        self.pause_records: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []
        self.previous_pauses: Any = None
        self.funding_index = 0

    def _slippage(self, price: Price, qty: Qty) -> Price:
        sign = 1 if qty.value > 0 else -1
        return Price(price.value * (1 + sign * self.run.profile.slippage))

    def _trace(self, stage: str, symbol: Symbol | None = None) -> None:
        self.trace.append({"at": self.clock.now(), "stage": stage, "symbol": symbol})

    def _history(self, symbol: Symbol, interval: Interval, at: datetime, n: int) -> ClosedBars:
        return self.data.histories[(symbol, interval)].through(at - interval.duration, lookback=n)

    def _warmup(self) -> None:
        for symbol in self.run.symbols:
            history = self.data.histories[(symbol, Interval.DAY)]
            for bar in history.bars:
                if (
                    bar.end >= self.run.start
                    if Interval.DAY.aligned(self.run.start)
                    else bar.end > self.run.start
                ):
                    break
                # Dispatch is type-specific below; no orders are generated during warm-up.
                prefix = history.through(bar.start, lookback=candidates.required_bars(self.run.s1))
                if isinstance(self.run.s1, S1AParams):
                    output = candidates.s1_a(prefix, self.states[symbol], self.run.s1)
                else:
                    output = candidates.s1_b(prefix, self.states[symbol], self.run.s1)
                self.states[symbol] = output.state

    async def _figures(self) -> dict[StrategyId, StrategyRiskInput]:
        async with self.db.unit_of_work() as uow:
            books = uow.portfolio.books()
        result = {}
        for strategy in StrategyId:
            pnl = (
                sum(
                    (
                        b.net_realized
                        + b.qty * (self.marks[b.symbol].value - (b.avg_price or ZERO))
                        for b in books
                        if b.strategy is strategy
                    ),
                    ZERO,
                )
                - self.daily_base[strategy]
            )
            streak = 0
            last_loss = None
            closed = sorted(
                (t for t in self.ledger.trades if t.closed_at is not None),
                key=lambda t: (t.closed_at, t.episode_id),
            )
            for trade in reversed(closed):
                if trade.strategy is not strategy or trade.closed_at is None:
                    continue
                if trade.net_pnl >= 0:
                    break
                if last_loss is None:
                    last_loss = trade.closed_at
                streak += 1
            result[strategy] = StrategyRiskInput(pnl, streak, last_loss)
        return result

    async def _settle(self) -> None:
        await self.sync.tick()
        async with self.db.unit_of_work() as uow:
            self.ledger.ingest(uow)

    async def _observe(self) -> None:
        snapshot = await self.venue.get_account()
        assert not isinstance(snapshot, ExchangeError)
        midnight = Interval.DAY.floor(self.clock.now())
        if midnight > self.tracker.day_start:
            self.tracker.start_day(snapshot)
            async with self.db.unit_of_work() as uow:
                self.daily_base = {
                    s: sum((b.net_realized for b in uow.portfolio.books() if b.strategy is s), ZERO)
                    for s in StrategyId
                }
            self._trace("start_day")
        else:
            self.tracker.observe(snapshot)
        self.gate.refresh_pauses(await self._figures())
        self._trace("risk")
        active = self.pauses.to_json()
        if active != self.previous_pauses:
            self.pause_records.append({"at": self.clock.now(), "active": active})
            self.previous_pauses = active
        self.equity.append(
            {
                "at": self.clock.now(),
                "wallet_balance": snapshot.wallet_balance,
                "unrealized_pnl": snapshot.unrealized_pnl,
                "equity": snapshot.equity,
                "day_start": self.tracker.day_start,
                "baseline": self.tracker.state.baseline,
                "daily_pnl": self.tracker.daily_pnl,
                "risk_peak": self.tracker.risk_peak,
            }
        )
        # Report attribution must account for every cash movement, not invent a second book.
        net = sum((t.net_pnl for t in self.ledger.trades), ZERO)
        unrealized = sum(
            (
                t.remaining
                * (self.marks[t.symbol].value - t.entry_notional / t.entry_qty)
                * (1 if t.direction == "LONG" else -1)
                for t in self.ledger.active.values()
            ),
            ZERO,
        )
        # The venue uses a shared average price; per-strategy realized allocation
        # differs until flat, but total equity must always reconcile.
        if abs(snapshot.equity - (self.run.profile.start_equity + net + unrealized)) > Decimal(
            "1e-18"
        ):
            raise ValueError("report trade ledger does not reconcile with exchange equity")

    async def _signal(self, signal: MarketSignal) -> None:
        symbol = signal.symbol
        tier = self.data.tiers[symbol]
        market = MarketInputs(
            self.marks,
            self.data.instruments[symbol].to_spec(),
            min(
                self.run.profile.limits.max_leverage,
                tier.max_leverage,
                self.data.instruments[symbol].max_leverage,
            ),
            tier.mmr,
            self.run.profile.taker_fee,
            self.run.profile.slippage,
            tier.limit_notional,
        )
        async with self.db.unit_of_work() as uow:
            episode = uow.episodes.active_for(signal.strategy, symbol)
        if episode is not None and (
            signal.kind == "EXIT" or signal.direction is not episode.direction
        ):
            reason = "trend_change"
            if signal.kind == "EXIT" and signal.strategy is StrategyId.S1:
                reason = "channel_exit"
            elif signal.strategy is StrategyId.S2:
                assert self.run.s2 is not None
                trade = self.ledger.active.get((StrategyId.S2, symbol))
                if self.states[symbol].trend.value != episode.direction.value:
                    reason = "trend_change"
                elif (
                    trade is not None
                    and (
                        self.clock.now()
                        - (
                            Interval.FOUR_HOUR
                            if isinstance(self.run.s2, S2PParams)
                            else Interval.HOUR
                        ).floor(trade.opened_at)
                    )
                    // (4 * HOUR if isinstance(self.run.s2, S2PParams) else HOUR)
                    > self.run.s2.max_hold_bars
                ):
                    reason = "max_holding"
                else:
                    reason = (
                        "price_target" if isinstance(self.run.s2, S2PParams) else "mean_reversion"
                    )
            self.ledger.exit_reasons.setdefault(episode.episode_id, reason)
        admission = await self.router.handle(signal, market, await self._figures())
        if admission.order is not None and not admission.reused:
            response = await self.sync.submit(admission.order)
            if isinstance(response, ExchangeError):
                raise ValueError("normal backtest submission failed: " + response.message)
        if signal.strategy is StrategyId.S1 and signal.kind != "EXIT":
            async with self.db.unit_of_work() as uow:
                book = uow.portfolio.book(StrategyId.S1, symbol)
                if book.qty * signal.direction.sign > 0:
                    self.portfolio.trail_stop(
                        uow, StrategyId.S1, symbol, market.instrument, signal.stop_price
                    )
                    self.portfolio.reconcile(uow)
                await uow.commit()

    async def _signals(self) -> None:
        at = self.clock.now()
        if Interval.DAY.aligned(at):
            for symbol in self.run.symbols:
                self._trace("signal:S1", symbol)
                h = self._history(symbol, Interval.DAY, at, candidates.required_bars(self.run.s1))
                if isinstance(self.run.s1, S1AParams):
                    out = candidates.s1_a(h, self.states[symbol], self.run.s1)
                else:
                    out = candidates.s1_b(h, self.states[symbol], self.run.s1)
                self.states[symbol] = out.state
                if out.signal:
                    await self._signal(out.signal)
        if self.run.s2 is None:
            return
        for symbol in self.run.symbols:
            if isinstance(self.run.s2, S2PParams):
                if not Interval.FOUR_HOUR.aligned(at):
                    continue
                self._trace("signal:S2", symbol)
                hourly = self._history(
                    symbol, Interval.HOUR, at, candidates.required_bars(self.run.s2) * 4
                )
                h4 = ClosedBars(
                    symbol, Interval.FOUR_HOUR, four_hour_bars(hourly.bars, as_of=at), at
                )
                pending = None
                async with self.db.unit_of_work() as uow:
                    active = uow.episodes.active_for(StrategyId.S2, symbol)
                    book = uow.portfolio.book(StrategyId.S2, symbol)
                    if active is not None:
                        order = uow.orders.get(active.entry_order_keys[0])
                        assert order is not None and order.attached_stop is not None
                        trade = self.ledger.active.get((StrategyId.S2, symbol))
                        pending = S2PEpisode(
                            symbol,
                            active.direction,
                            S2PState.OPEN if book.qty else S2PState.PENDING,
                            Price(book.avg_price) if book.avg_price is not None else None,
                            order.attached_stop.stop_loss,
                            Interval.FOUR_HOUR.floor(trade.opened_at)
                            if trade and book.qty
                            else None,
                        )
                out_p = candidates.s2_p(h4, self.states[symbol], pending, self.run.s2)
                if out_p.signal:
                    await self._signal(out_p.signal)
                continue
            self._trace("signal:S2", symbol)
            h = self._history(symbol, Interval.HOUR, at, candidates.required_bars(self.run.s2))
            trade = self.ledger.active.get((StrategyId.S2, symbol))
            episode = None
            if trade is not None:
                episode = S2Episode(
                    symbol,
                    Direction(trade.direction),
                    Interval.HOUR.floor(trade.opened_at),
                    Price(trade.initial_stop),
                )
            if isinstance(self.run.s2, S2AParams):
                output = candidates.s2_a(h, self.states[symbol], episode, self.run.s2)
            else:
                output = candidates.s2_b(h, self.states[symbol], episode, self.run.s2)
            if output.signal:
                await self._signal(output.signal)
        await self._settle()

    async def _finish_entries(self) -> None:
        self.pauses.add(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "end_of_range", self.clock.now()))
        async with self.db.unit_of_work() as uow:
            for episode in uow.episodes.all():
                if episode.state is not EpisodeState.CLOSED:
                    self.ledger.exit_reasons.setdefault(episode.episode_id, "end_of_range")
                    self.portfolio.request_close(uow, episode)
            self.portfolio.reconcile(uow)
            await uow.commit()
        self._trace("end_close")
        await self._settle()

    async def execute(self, metadata: dict[str, Any]) -> BacktestResult:
        self._warmup()
        try:
            # Prime marks from the last pre-range bar; there are no pre-range positions.
            for symbol in self.run.symbols:
                bar = self._history(symbol, Interval.HOUR, self.run.start, 1).bars[-1]
                self.venue.advance_bar(bar)
                self.marks[symbol] = Price(bar.close)
            if not await self.sync.synchronize():
                raise ValueError("initial account synchronization failed")
            at = self.run.start
            while at <= self.run.end:
                self.clock.set(at)
                self._trace("fills")
                if at > self.run.start:
                    for symbol in self.run.symbols:
                        bar = self._history(symbol, Interval.HOUR, at, 1).bars[-1]
                        rate = self.data.funding.get((symbol, at))
                        self.venue.advance_bar(bar, funding=(rate,) if rate else ())
                        self.marks[symbol] = Price(bar.close)
                await self._settle()
                self._trace("funding")
                payments = self.venue.funding_payments
                for payment in payments[self.funding_index :]:
                    cost = -payment.amount
                    await self.portfolio.funding(
                        payment.record.symbol.value + payment.record.funding_time.isoformat(),
                        payment.record.symbol,
                        cost,
                    )
                    self.ledger.funding(payment.record.symbol, cost)
                self.funding_index = len(payments)
                await self._observe()
                if at == self.run.end:
                    break
                if at == self.run.end - HOUR:
                    await self._finish_entries()
                else:
                    await self._signals()
                at += HOUR
            async with self.db.unit_of_work() as uow:
                remaining = tuple(
                    (s, symbol, qty) for s, symbol, qty in uow.positions.all() if qty.value
                )
                return BacktestResult(
                    "INCOMPLETE" if remaining else "COMPLETED",
                    (),
                    tuple(t.record() for t in self.ledger.trades),
                    uow.orders.all(),
                    uow.fills.all(),
                    uow.decisions.all(),
                    tuple(self.equity),
                    tuple(self.pause_records),
                    tuple(self.trace),
                    metadata,
                    remaining,
                )
        finally:
            await self.sync.close()
