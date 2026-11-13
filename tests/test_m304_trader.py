"""Deterministic assembly tests. No wall clock, socket, credentials, or market DB."""

import asyncio
from datetime import timedelta
from decimal import Decimal as D
from itertools import permutations
from types import SimpleNamespace

import pytest
from test_control import setup
from test_portfolio import POLICY
from test_risk import NOW, counter, limits

from richletsgo.domain import Environment, StrategyId, Symbol
from richletsgo.live.trader import FundingPayment, LoopPolicy, Trader
from richletsgo.marketdata.models import Bar, Interval
from richletsgo.portfolio.models import StrategyBook
from richletsgo.portfolio.service import PortfolioService
from richletsgo.risk.equity import AccountSnapshot, EquityTracker
from richletsgo.risk.gate import RiskGate

T = NOW.replace(hour=0)
SYMBOLS = (Symbol.BTCUSDT, Symbol.ETHUSDT)


def candle(symbol, interval, at=T):
    return Bar(
        symbol, interval, at - interval.duration, D(100), D(101), D(99), D(100), D(1), D(100)
    )


async def runtime(tmp_path, backend="memory"):
    db, control, _, clock, _ = await setup(tmp_path, backend)
    clock.set(T)
    gate = RiskGate(
        account_id="acct",
        environment=Environment.DEMO,
        uow_factory=db.unit_of_work,
        limits=limits(),
        tracker=EquityTracker.open(AccountSnapshot(T - timedelta(hours=1), D(1000), D(0))),
        pauses=control.pauses,
        clock=clock,
        unique_factory=counter(),
    )
    control.tracker = gate.tracker
    control._initial_tracker_state = gate.tracker.state
    control.lock = gate.account_lock
    portfolio = PortfolioService(db.unit_of_work, clock, counter(), POLICY)

    class Sync:
        ticks = 0
        stable = True
        sender = SimpleNamespace(pending_entries={})
        policy = SimpleNamespace(history_window=timedelta(days=7))
        gateway = SimpleNamespace(connected=True)

        async def synchronize(self):
            return self.stable

        async def tick(self):
            self.ticks += 1

        async def _refresh(self, symbol, since):
            return ()

        async def _drain(self):
            pass

        async def confirm_fills(self, since):
            return True

        async def close(self):
            pass

    class Inputs:
        balance = D(1000)
        payments = ()
        seen = []
        fallback = True
        account_reads = 0

        async def warmup(self):
            return ()

        async def bar(self, symbol, interval, at):
            return candle(symbol, interval, at) if self.fallback else None

        async def funding(self, since):
            return self.payments

        async def account(self):
            self.account_reads += 1
            return AccountSnapshot(clock.now(), self.balance, D(0))

        async def cash_flows(self, since):
            return ()

        async def maintain(self):
            pass

        async def market(self, symbol):
            return None

        async def check_liquidation(self):
            pass

        async def signals(self, u, at, bars, available):
            self.seen.append((at, tuple(sorted(s.value for s in available))))
            return ()

        async def figures(self, u, baseline):
            return {}

    async def resolve(txn):
        return True

    async def recover():
        pass

    sync, inputs = Sync(), Inputs()
    alerts = []
    trader = Trader(
        uow=db.unit_of_work,
        clock=clock,
        gate=gate,
        portfolio=portfolio,
        synchronizer=sync,
        control=control,
        inputs=inputs,
        policy=LoopPolicy(timedelta(seconds=1), timedelta(seconds=90), timedelta(hours=2)),
        symbols=SYMBOLS,
        resolve_commit=resolve,
        recover=recover,
        config_record="{}",
        alert=alerts.append,
    )
    return db, clock, trader, inputs, sync, alerts


async def virtual_runtime(tmp_path, backend="memory"):
    from test_portfolio import RISK_INPUT
    from test_risk import instrument, market

    from richletsgo.domain import Price
    from richletsgo.exchange.simulated import SimulatedExchange
    from richletsgo.oms.journal import ProtectiveJournal
    from richletsgo.oms.synchronizer import Synchronizer, SyncPolicy
    from richletsgo.portfolio.signals import AiMode, AiScoreStore, MarketSignal, SignalRouter
    from richletsgo.risk.records import Direction
    from richletsgo.strategy.boundary import boundary_signals

    db, clock, trader, inputs, _, alerts = await runtime(tmp_path, backend)
    trader.gate.limits = limits(risk_per_trade=D(".005"))
    exchange = SimulatedExchange(
        instruments=[instrument(s) for s in SYMBOLS],
        clock=clock,
        wallet_balance=D(1000),
        maker_fee=D(0),
        taker_fee=D(0),
        slippage=lambda p, q: p,
    )
    sync = Synchronizer(
        db.unit_of_work,
        exchange,
        trader.portfolio,
        ProtectiveJournal(tmp_path / "journal"),
        clock,
        trader.gate.pauses,
        counter(),
        SyncPolicy(
            timedelta(days=6), timedelta(minutes=5), timedelta(minutes=2), timedelta(seconds=5), 3
        ),
        SYMBOLS,
    )
    sync.sender.entry_ready = lambda: trader.gate.entry_ready()
    sync.sender.entry_allowed = lambda order: (
        trader.gate.entry_ready() and not trader.gate.pauses.blocking(order.strategy, order.symbol)
    )
    trader.sync = sync
    router = SignalRouter(
        trader.gate,
        trader.portfolio,
        AiScoreStore(),
        mode=AiMode.DISABLED,
        threshold=D(".5"),
        factor=lambda v: v,
        trigger_by="LastPrice",
    )

    async def signals(u, at, bars, available):
        result = []

        async def emit(strategy, symbol):
            if symbol in available:
                result.append(
                    MarketSignal(
                        strategy,
                        symbol,
                        T - timedelta(hours=1),
                        "ENTRY",
                        Direction.LONG,
                        Price(D(100)),
                        Price(D(95)),
                    )
                )

        async def first(s):
            await emit(StrategyId.S1, s)

        async def second(s):
            await emit(StrategyId.S2, s)

        async def settle():
            pass

        await boundary_signals(at, SYMBOLS, object(), first, second, settle)
        return tuple(result)

    async def figures(u, baseline):
        return RISK_INPUT

    async def markets(symbol):
        from dataclasses import replace

        return replace(
            market(),
            instrument=instrument(symbol),
            marks={s: Price(D(100)) for s in SYMBOLS},
            observed_at=clock.now(),
        )

    inputs.signals, inputs.figures, inputs.market, inputs.route = (
        signals,
        figures,
        markets,
        router.handle,
    )
    return db, clock, trader, inputs, exchange, sync


def test_virtual_exchange_all_midnight_permutations_same_decisions_and_orders(tmp_path):
    async def run():
        results = []
        bars = tuple(candle(s, i) for i in (Interval.HOUR, Interval.DAY) for s in SYMBOLS)
        for index, ordering in enumerate(permutations(bars)):
            db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path / str(index))
            try:
                assert await trader.boot()
                for bar in ordering:
                    await trader.accept(bar)
                    await trader.step()
                opened = tuple(
                    [
                        (o.order_link_id, o.symbol, o.order_qty)
                        for symbol in SYMBOLS
                        for o in await exchange.get_open_orders(symbol)
                    ]
                )
                assert opened
                async with db.unit_of_work() as u:
                    decisions = tuple(
                        (d.signal_id, d.kind, d.approved_qty) for d in u.decisions.all()
                    )
                assert len(decisions) == 4
                results.append((opened, decisions))
                await trader.accept(ordering[0])
                await trader.step()
                assert (
                    tuple(
                        [
                            (o.order_link_id, o.symbol, o.order_qty)
                            for symbol in SYMBOLS
                            for o in await exchange.get_open_orders(symbol)
                        ]
                    )
                    == opened
                )
            finally:
                await trader.close()
        assert all(r == results[0] for r in results)

    asyncio.run(run())


def test_all_midnight_arrival_permutations_and_duplicate_are_equal(tmp_path):
    async def run():
        outcomes = []
        bars = tuple(candle(s, i) for i in (Interval.HOUR, Interval.DAY) for s in SYMBOLS)
        for ordering in permutations(bars):
            db, clock, trader, inputs, sync, alerts = await runtime(tmp_path)
            assert await trader.boot()
            for bar in ordering:
                await trader.accept(bar)
                await trader.step()
            await trader.accept(ordering[0])
            await trader.step()
            assert trader.last_boundary == T
            outcomes.append(inputs.seen)
        assert all(o == outcomes[0] for o in outcomes)

    asyncio.run(run())


@pytest.mark.parametrize("fallback", [False, True])
def test_bar_timeout_only_skips_missing_symbol(tmp_path, fallback):
    async def run():
        db, clock, trader, inputs, sync, alerts = await runtime(tmp_path)
        inputs.fallback = fallback
        assert await trader.boot()
        for interval in (Interval.HOUR, Interval.DAY):
            await trader.accept(candle(Symbol.BTCUSDT, interval))
        assert not await trader.process(T)
        clock.advance(timedelta(seconds=91))
        assert await trader.process(T)
        assert (
            inputs.seen[0][1] == ("BTCUSDT", "ETHUSDT")
            if fallback
            else inputs.seen[0][1] == ("BTCUSDT",)
        )

    asyncio.run(run())


def test_funding_wait_keeps_protection_and_control_tick_running(tmp_path):
    async def run():
        db, clock, trader, inputs, sync, alerts = await runtime(tmp_path)
        async with db.unit_of_work() as u:
            u.portfolio.put_book(StrategyBook(StrategyId.S1, Symbol.BTCUSDT, D(1), D(100)))
            await u.commit()
        ticks = []
        original = trader.control.tick

        async def tick():
            ticks.append(sync.ticks)
            await original()

        trader.control.tick = tick
        assert await trader.boot()
        for interval in (Interval.HOUR, Interval.DAY):
            for symbol in SYMBOLS:
                await trader.accept(candle(symbol, interval))
        assert not await trader.process(T)
        assert trader.funding_wait and not trader.gate.entry_ready()
        assert trader.gate.tracker.day_start < T
        before = sync.ticks
        clock.advance(timedelta(seconds=2))
        await trader.maintenance()
        assert sync.ticks > before and ticks[-1] == sync.ticks
        inputs.payments = (FundingPayment("f", Symbol.BTCUSDT, T, D("0.1")),)
        assert await trader.process(T)
        assert trader.gate.tracker.day_start == T and trader.gate.entry_ready()

    asyncio.run(run())


def test_prepared_account_survives_restart_without_resampling(tmp_path, monkeypatch):
    async def run():
        db, clock, trader, inputs, sync, alerts = await runtime(tmp_path)
        assert await trader.boot()
        for interval in (Interval.HOUR, Interval.DAY):
            for symbol in SYMBOLS:
                await trader.accept(candle(symbol, interval))
        cls = type(db.unit_of_work())
        original = cls.commit
        fired = False

        async def crash(u):
            nonlocal fired
            prepared = u.recovery.get("boundary_input:" + T.isoformat() + ":account") is not None
            marked = u.recovery.get("boundary_checkpoint:" + T.isoformat() + ":account") is not None
            await original(u)
            if prepared and not marked and not fired:
                fired = True
                raise RuntimeError("after prepare commit")

        monkeypatch.setattr(cls, "commit", crash)
        with pytest.raises(RuntimeError):
            await trader.process(T)
        assert inputs.account_reads == 1
        inputs.balance = D(700)
        assert await trader.process(T)
        assert inputs.account_reads == 1
        assert trader.gate.tracker.state.baseline == 1000

    asyncio.run(run())


def test_failed_boot_never_opens_gate_and_health_contains_no_config(tmp_path):
    async def run():
        db, clock, trader, inputs, sync, alerts = await runtime(tmp_path)
        sync.stable = False
        assert not await trader.boot()
        assert not trader.gate.entry_ready()
        assert "config" not in trader.health() and "token" not in str(trader.health())
        await trader.close()
        assert not trader.health()["alive"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "stage", ["input", "fills", "funding", "prepare", "account", "output", "decision", "signals"]
)
@pytest.mark.parametrize("after", [False, True])
def test_each_boundary_commit_replays_same_output(tmp_path, monkeypatch, stage, after):
    from richletsgo.storage.codec import decode

    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        assert await trader.boot()
        for interval in (Interval.HOUR, Interval.DAY):
            for symbol in SYMBOLS:
                await trader.accept(candle(symbol, interval))
        cls, fired = type(db.unit_of_work()), False
        original = cls.commit
        checkpoint = "boundary_checkpoint:" + T.isoformat() + ":"
        keys = {
            "input": "boundary_input:" + T.isoformat(),
            "prepare": "boundary_input:" + T.isoformat() + ":account",
            "output": "boundary_output:" + T.isoformat(),
        }
        target = keys.get(stage, checkpoint + stage)

        async def crash(u):
            nonlocal fired
            # Fire only at the first UoW that introduces the target, not a later read.
            introduced = (
                u.recovery.get(target) is not None and target not in db._state.recovery_data
            )
            if stage == "decision":
                introduced = bool(u.decisions.all()) and not db._state.decisions
            if introduced and not fired:
                fired = True
                if after:
                    await original(u)
                raise RuntimeError("synthetic process termination")
            await original(u)

        monkeypatch.setattr(cls, "commit", crash)
        with pytest.raises(RuntimeError):
            await trader.process(T)
        assert fired
        # Restart memory state from committed recovery, as a new process does.
        async with db.unit_of_work() as u:
            raw = u.recovery.get("equity_tracker")
            if raw:
                trader.gate.tracker = EquityTracker(decode(raw))
        await sync.close()
        sync = type(sync)(
            db.unit_of_work,
            exchange,
            trader.portfolio,
            sync.journal,
            clock,
            trader.gate.pauses,
            counter(),
            sync.policy,
            SYMBOLS,
        )
        sync.sender.entry_ready = lambda: trader.gate.entry_ready()
        trader.sync = sync
        assert await trader.boot()
        assert await trader.process(T)
        async with db.unit_of_work() as u:
            assert len(decode(u.recovery.get("boundary_output:" + T.isoformat()))) == 4
            assert len(u.decisions.all()) == 4
            assert u.recovery.get(checkpoint + "signals") == "true"
        assert trader.gate.tracker.state.baseline == D(1000)
        assert trader.last_boundary == T
        for symbol in SYMBOLS:
            assert len(await exchange.get_open_orders(symbol)) == 2
        await trader.close()

    asyncio.run(run())


def test_next_boundary_arrives_first_and_account_prepare_before_commit_resamples(
    tmp_path, monkeypatch
):
    async def run():
        db, clock, trader, inputs, sync, alerts = await runtime(tmp_path)
        assert await trader.boot()
        await trader.accept(candle(SYMBOLS[0], Interval.HOUR, T + timedelta(hours=1)))
        for interval in (Interval.HOUR, Interval.DAY):
            for symbol in SYMBOLS:
                await trader.accept(candle(symbol, interval))
        cls = type(db.unit_of_work())
        original, fired = cls.commit, False

        async def crash(u):
            nonlocal fired
            if not fired and u.recovery.get("boundary_input:" + T.isoformat() + ":account"):
                fired = True
                raise RuntimeError("before prepare commit")
            await original(u)

        monkeypatch.setattr(cls, "commit", crash)
        with pytest.raises(RuntimeError):
            await trader.step()
        inputs.balance = D(900)
        await trader.step()
        assert trader.last_boundary == T
        assert T + timedelta(hours=1) in trader._boundaries
        assert trader.gate.tracker.state.baseline == D(900)

    asyncio.run(run())


def test_observation_commit_unknown_keeps_old_memory_until_marker_and_reconcile(
    tmp_path, monkeypatch
):
    from richletsgo.storage.errors import CommitOutcomeUnknown

    async def run():
        db, clock, trader, inputs, sync, alerts = await runtime(tmp_path)
        assert await trader.boot()
        for i in (Interval.HOUR, Interval.DAY):
            for s in SYMBOLS:
                await trader.accept(candle(s, i))
        assert await trader.process(T)
        cls, old = type(db.unit_of_work()), trader.gate.tracker.state
        original, fired = cls.commit, False

        async def uncertain(u):
            nonlocal fired
            await original(u)
            if not fired:
                fired = True
                raise CommitOutcomeUnknown("synthetic-marker")

        monkeypatch.setattr(cls, "commit", uncertain)
        with pytest.raises(CommitOutcomeUnknown):
            await trader.observe(AccountSnapshot(T, D(900), D(0)), ())
        assert trader.gate.tracker.state == old and not trader.gate.entry_ready()
        await trader.maintenance()
        assert trader.gate.tracker.risk_peak == D(1000)
        assert trader.gate.tracker.equity == D(1000)  # subsequent fresh account observation
        assert trader._pending_txn is None

    asyncio.run(run())


@pytest.mark.parametrize("committed", [False, True])
def test_sqlite_prepare_unknown_uses_marker_before_resampling(tmp_path, committed):
    from richletsgo.storage.errors import CommitOutcomeUnknown
    from richletsgo.storage.sqlite import open_readonly

    async def run():
        db, clock, trader, inputs, sync, alerts = await runtime(tmp_path, "sqlite")
        trader.resolve_commit = db.resolve

        async def recover():
            assert await db.recover(sync.synchronize)

        trader.recover = recover
        assert await trader.boot()
        for interval in (Interval.HOUR, Interval.DAY):
            for symbol in SYMBOLS:
                await trader.accept(candle(symbol, interval))
        fired = False

        def fault(stage, connection):
            nonlocal fired
            prepared = connection.execute(
                "SELECT 1 FROM boundary_inputs WHERE key=?",
                ("boundary_input:" + T.isoformat() + ":account",),
            ).fetchone()
            if prepared and not fired and stage == ("after_commit" if committed else "commit"):
                fired = True
                raise OSError("uncertain prepare")

        db.fault = fault
        with pytest.raises(CommitOutcomeUnknown):
            await trader.process(T)
        assert not trader.gate.entry_ready() and inputs.account_reads == 1
        db.fault = None
        inputs.balance = D(900)
        await trader.maintenance()
        assert not db.entry_paused
        assert await trader.process(T)
        assert inputs.account_reads == (1 if committed else 2)
        assert trader.gate.tracker.state.baseline == (D(1000) if committed else D(900))
        c = open_readonly(db.path)
        try:
            assert c.execute("SELECT COUNT(*) FROM config_versions").fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM boundary_inputs").fetchone()[0] == 2
            assert c.execute("SELECT COUNT(*) FROM position_snapshots").fetchone()[0] == 2
        finally:
            c.close()

    asyncio.run(run())


def test_initial_tracker_waits_for_funding_and_failed_reconnect_stays_closed(tmp_path):
    async def run():
        db, clock, trader, inputs, sync, alerts = await runtime(tmp_path)
        trader.initialize_tracker = True
        async with db.unit_of_work() as u:
            u.portfolio.put_book(StrategyBook(StrategyId.S1, SYMBOLS[0], D(1), D(100)))
            await u.commit()
        assert await trader.boot()
        for interval in (Interval.HOUR, Interval.DAY):
            for symbol in SYMBOLS:
                await trader.accept(candle(symbol, interval))
        assert not await trader.process(T)
        assert inputs.account_reads == 0
        inputs.payments = (FundingPayment("initial-funding", SYMBOLS[0], T, D(1)),)
        inputs.balance = D(999)
        assert await trader.process(T)
        assert trader.gate.tracker.state.baseline == D(999)

        async def broken_warmup():
            raise OSError("synthetic unavailable history")

        inputs.warmup = broken_warmup
        assert not await trader.boot()
        clock.advance(timedelta(seconds=61))
        await trader.maintenance()
        assert not trader.gate.entry_ready()

    asyncio.run(run())


def test_undecided_signal_uses_new_equity_and_pause_and_reuses_decision(tmp_path):
    from richletsgo.risk.pause import Pause, PauseKind
    from richletsgo.storage.codec import decode

    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        try:
            assert await trader.boot()
            for i in (Interval.HOUR, Interval.DAY):
                for s in SYMBOLS:
                    await trader.accept(candle(s, i))
            original = inputs.route

            async def interrupted(*args):
                raise RuntimeError("after output before decision")

            inputs.route = interrupted
            with pytest.raises(RuntimeError):
                await trader.process(T)
            inputs.route = original
            inputs.balance = D(900)
            trader.gate.pauses.add(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "operator", T))
            assert await trader.process(T)
            async with db.unit_of_work() as u:
                decisions = u.decisions.all()
                assert len(decisions) == 4 and all(d.kind.value == "REJECTED" for d in decisions)
                assert all(dict(d.basis)["runtime_equity"] == "900" for d in decisions)
                assert all("operator" in dict(d.basis)["pauses"] for d in decisions)
                generated = decode(u.recovery.get("boundary_output:" + T.isoformat()))
            market = await inputs.market(SYMBOLS[0])
            admission = await inputs.route(generated[0], market, await trader._figures())
            assert admission.reused and admission.decision == decisions[0]
            for symbol in SYMBOLS:
                assert not await exchange.get_open_orders(symbol)
        finally:
            await trader.close()

    asyncio.run(run())


@pytest.mark.parametrize("committed", [False, True])
def test_live_sqlite_entry_unknown_reconciles_then_sends_exactly_once(tmp_path, committed):
    from richletsgo.domain import Price
    from richletsgo.portfolio.signals import MarketSignal
    from richletsgo.risk.records import Direction

    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path, "sqlite")
        trader.resolve_commit = db.resolve

        async def recover():
            assert await db.recover(sync.synchronize)

        trader.recover = recover
        try:
            assert await trader.boot()
            trader.gate.tracker.publish(EquityTracker.open(AccountSnapshot(T, D(1000), D(0))))
            signal = MarketSignal(
                StrategyId.S1,
                SYMBOLS[0],
                T - timedelta(hours=1),
                "ENTRY",
                Direction.LONG,
                Price(D(100)),
                Price(D(95)),
            )
            admission = await inputs.route(
                signal, await inputs.market(SYMBOLS[0]), await trader._figures()
            )
            assert admission.order is not None
            fired = False

            def fault(stage, connection):
                nonlocal fired
                if stage == ("after_commit" if committed else "commit") and not fired:
                    fired = True
                    raise OSError("send commit uncertain")

            db.fault = fault
            await sync.submit(admission.order)
            assert sync.sender.pending_entries
            assert not await exchange.get_open_orders(SYMBOLS[0])
            db.fault = None
            await trader.maintenance()
            assert not db.entry_paused and not sync.sender.pending_entries
            orders = await exchange.get_open_orders(SYMBOLS[0])
            assert len(orders) == int(committed)
            if committed:
                assert orders[0].order_link_id == admission.order.order_link_id
            clock.advance(timedelta(seconds=1))
            await trader.maintenance()
            assert len(await exchange.get_open_orders(SYMBOLS[0])) == int(committed)
        finally:
            await trader.close()

    asyncio.run(run())


@pytest.mark.parametrize("committed", [False, True])
def test_risk_decision_unknown_resolves_without_duplicate_entry(tmp_path, committed):
    from richletsgo.storage.errors import CommitOutcomeUnknown

    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path, "sqlite")
        trader.resolve_commit = db.resolve

        async def recover():
            assert await db.recover(sync.synchronize)

        trader.recover = recover
        try:
            assert await trader.boot()
            for i in (Interval.HOUR, Interval.DAY):
                for s in SYMBOLS:
                    await trader.accept(candle(s, i))
            fired = False

            def fault(stage, connection):
                nonlocal fired
                decisions = connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
                if decisions and not fired and stage == ("after_commit" if committed else "commit"):
                    fired = True
                    raise OSError("decision commit uncertain")

            db.fault = fault
            with pytest.raises(CommitOutcomeUnknown):
                await trader.process(T)
            assert trader._pending_txn is not None and not trader.gate.entry_ready()
            db.fault = None
            await trader.maintenance()
            assert await trader.process(T)
            async with db.unit_of_work() as u:
                assert len(u.decisions.all()) == 4
            for symbol in SYMBOLS:
                assert len(await exchange.get_open_orders(symbol)) == 2
        finally:
            await trader.close()

    asyncio.run(run())


def test_real_reconciliation_system_pause_changes_do_not_block_committed_day_publish(tmp_path):
    from richletsgo.storage.errors import CommitOutcomeUnknown

    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path, "sqlite")
        trader.resolve_commit = db.resolve

        async def recover():
            assert await db.recover(sync.synchronize)

        trader.recover = recover
        try:
            assert await trader.boot()
            for i in (Interval.HOUR, Interval.DAY):
                for s in SYMBOLS:
                    await trader.accept(candle(s, i))
            fired = False

            def fault(stage, connection):
                nonlocal fired
                exists = connection.execute(
                    "SELECT 1 FROM boundary_checkpoints WHERE key=?",
                    ("boundary_checkpoint:" + T.isoformat() + ":account",),
                ).fetchone()
                if exists and not fired and stage == "after_commit":
                    fired = True
                    raise OSError("day commit uncertain")

            db.fault = fault
            with pytest.raises(CommitOutcomeUnknown):
                await trader.process(T)
            assert trader.gate.tracker.day_start < T
            db.fault = None
            await trader.maintenance()
            assert trader.gate.tracker.day_start == T
            assert not any(
                p.reason == "boundary_state_reconcile" for p in trader.gate.pauses.active()
            )
            assert await trader.process(T)
        finally:
            await trader.close()

    asyncio.run(run())


def test_first_account_queries_current_day_and_does_not_count_old_deposit_twice(tmp_path):
    from richletsgo.risk.equity import CashFlow

    async def run():
        db, clock, trader, inputs, sync, _ = await runtime(tmp_path)
        trader.initialize_tracker = True
        trader.gate.tracker.publish(
            EquityTracker.open(AccountSnapshot(T - timedelta(days=10), D(0), D(0)))
        )
        flow = CashFlow("first-deposit", T, D(100))

        async def flows(since):
            assert since == T
            return (flow,)

        inputs.cash_flows = flows
        assert await trader.boot()
        assert not trader.gate.entry_ready()
        for i in (Interval.HOUR, Interval.DAY):
            for s in SYMBOLS:
                await trader.accept(candle(s, i))
        assert await trader.process(T)
        await trader.observe(AccountSnapshot(T, D(1000), D(0)), (flow,))
        assert trader.gate.tracker.daily_pnl == 0
        assert trader.gate.tracker.state.net_deposit == 0
        assert "first-deposit" in trader.gate.tracker.state.seen_flows

    asyncio.run(run())


def test_gate_rechecks_market_deadline_after_waiting_for_account_lock(tmp_path):
    from dataclasses import replace

    from richletsgo.domain import Price
    from richletsgo.portfolio.signals import MarketSignal
    from richletsgo.risk.records import Direction

    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        try:
            assert await trader.boot()
            trader.gate.tracker.publish(EquityTracker.open(AccountSnapshot(T, D(1000), D(0))))
            signal = MarketSignal(
                StrategyId.S1,
                SYMBOLS[0],
                T - timedelta(hours=1),
                "ENTRY",
                Direction.LONG,
                Price(D(100)),
                Price(D(95)),
            )
            market = replace(await inputs.market(SYMBOLS[0]), valid_until=T + timedelta(seconds=1))
            figures = await trader._figures()
            async with trader.gate.account_lock:
                task = asyncio.create_task(inputs.route(signal, market, figures))
                await asyncio.sleep(0)
                assert not task.done()
                clock.advance(timedelta(seconds=2))
            result = await task
            assert result.order is None
            assert result.decision.rejections[0].detail == "stale market inputs"
        finally:
            await trader.close()

    asyncio.run(run())


def test_entry_resume_rechecks_pause_after_marker_wait(tmp_path):
    from dataclasses import replace

    from richletsgo.domain import OrderState, Price
    from richletsgo.portfolio.signals import MarketSignal
    from richletsgo.risk.pause import Pause, PauseKind
    from richletsgo.risk.records import Direction

    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        try:
            assert await trader.boot()
            trader.gate.tracker.publish(EquityTracker.open(AccountSnapshot(T, D(1000), D(0))))
            signal = MarketSignal(
                StrategyId.S1,
                SYMBOLS[0],
                T - timedelta(hours=1),
                "ENTRY",
                Direction.LONG,
                Price(D(100)),
                Price(D(95)),
            )
            admission = await inputs.route(
                signal, await inputs.market(SYMBOLS[0]), await trader._figures()
            )
            order = admission.order
            assert order is not None
            async with db.unit_of_work() as u:
                u.orders.put(replace(order, state=OrderState.SUBMITTED))
                await u.commit()
            sync.sender.pending_entries["marker"] = order

            async def resolve(txn):
                trader.gate.pauses.add(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "during-marker", T))
                return True

            result = await sync.sender.resume_entry_commit("marker", resolve)
            assert result.code.value == "TIMEOUT"
            assert "marker" in sync.sender.pending_entries
            assert not await exchange.get_open_orders(SYMBOLS[0])
        finally:
            await trader.close()

    asyncio.run(run())
