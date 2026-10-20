"""M1-03 risk engine. All numbers are test-only fixtures, not operating recommendations."""

import asyncio
import itertools
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from richletsgo.domain import (
    Environment,
    FixedClock,
    InstrumentSpec,
    OrderPurpose,
    OrderState,
    Price,
    Qty,
    StrategyId,
    Symbol,
)
from richletsgo.oms.exposure import exposure
from richletsgo.oms.models import OrderRecord
from richletsgo.risk.checks import StrategyRiskInput, check_limits, evaluate_pauses
from richletsgo.risk.equity import AccountSnapshot, CashFlow, EquityTracker
from richletsgo.risk.gate import EntryRequest, MarketInputs, RiskGate
from richletsgo.risk.guards import DATA_PAUSE_REASON, assess_market_data, authorize_command
from richletsgo.risk.limits import RiskLimits
from richletsgo.risk.pause import Pause, PauseKind, PauseState
from richletsgo.risk.records import (
    DecisionKind,
    Direction,
    EpisodeState,
    RejectionCode,
    signal_key,
)
from richletsgo.risk.resume import ResumePrompt, ResumeRecord, ResumeRejected, ResumeService
from richletsgo.risk.sizing import (
    SizingInput,
    approx_liquidation_price,
    liquidation_buffer_ok,
    size_entry,
)
from richletsgo.storage.memory import InMemoryDatabase, InMemoryUnitOfWork

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
MIDNIGHT = datetime(2026, 10, 20, tzinfo=UTC)
BTC, ETH = Symbol.BTCUSDT, Symbol.ETHUSDT
S1, S2 = StrategyId.S1, StrategyId.S2


def limits(**changes):
    base = dict(
        risk_per_trade=D("0.08"),
        daily_loss_limit_usdt=D("50"),
        max_drawdown_ratio=D("0.10"),
        strategy_allocations={S1: D("0.6"), S2: D("0.4")},
        consecutive_loss_limit=3,
        cooldown=timedelta(hours=6),
        max_leverage=D("5"),
        max_symbol_notional_usdt=D("1000"),
        max_account_notional_usdt=D("1000"),
        liquidation_buffer_ratio=D("0.5"),
        slippage_buffer=D("0"),
    )
    base.update(changes)
    return RiskLimits(**base)


def instrument(symbol=BTC, *, qty_step="0.001", min_qty="0.001", tick="0.01", max_lev="100"):
    return InstrumentSpec(symbol, D(qty_step), D(min_qty), D(tick), D(max_lev))


def sizing(**changes):
    base = dict(
        equity=D("1000"),
        direction=Direction.LONG,
        entry_price=Price(D("100")),
        stop_price=Price(D("95")),
        ai_factor=D("1"),
        fee_rate=D("0"),
        slippage=D("0"),
        instrument=instrument(),
        leverage=D("1"),
        mmr=D("0.005"),
    )
    base.update(changes)
    return SizingInput(**base)


# ---------------------------------------------------------------- RiskLimits


def test_risk_limits_have_no_defaults():
    with pytest.raises(TypeError):
        RiskLimits()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "changes",
    [
        {"risk_per_trade": D("0")},
        {"risk_per_trade": D("-0.01")},
        {"risk_per_trade": D("NaN")},
        {"daily_loss_limit_usdt": D("0")},
        {"max_drawdown_ratio": D("1")},
        {"max_leverage": D("0.5")},
        {"strategy_allocations": {S1: D("0.7"), S2: D("0.4")}},
        {"strategy_allocations": {S1: D("1")}},
        {"consecutive_loss_limit": 0},
        {"consecutive_loss_limit": True},
        {"cooldown": timedelta(0)},
        {"max_account_notional_usdt": D("-1")},
        {"liquidation_buffer_ratio": D("1")},
        {"slippage_buffer": D("-0.1")},
        {"risk_per_trade": 0.01},
    ],
)
def test_risk_limits_reject_out_of_range(changes):
    with pytest.raises((ValueError, TypeError)):
        limits(**changes)


# ---------------------------------------------------------------- sizing


@pytest.mark.parametrize("leverage", ["1", "2", "5"])
def test_leverage_changes_margin_not_quantity(leverage):
    result = size_entry(
        sizing(leverage=D(leverage), mmr=D("0")), limits(liquidation_buffer_ratio=D("0"))
    )
    assert result.ok
    # allowed 80 / distance 5 = 16
    assert result.qty == Qty(D("16"))
    assert D(dict(result.basis)["margin"]) == D("16") * D("100") / D(leverage)


def test_fees_and_slippage_widen_the_stop_distance():
    result = size_entry(sizing(fee_rate=D("0.001"), slippage=D("0.001")), limits())
    # distance = 5 + 0.002 * (100 + 95) = 5.39 -> 80 / 5.39 = 14.84... -> floor 14.842
    assert result.qty == Qty(D("14.842"))


def test_below_step_is_not_raised_to_minimum():
    tiny = sizing(equity=D("0.01"))
    result = size_entry(tiny, limits())
    assert result.reason is RejectionCode.BELOW_MIN_QTY and result.qty is None
    coarse = sizing(instrument=instrument(qty_step="1", min_qty="20"))
    assert size_entry(coarse, limits()).reason is RejectionCode.BELOW_MIN_QTY


def test_stop_is_aligned_away_from_entry_and_short_is_signed():
    lim = limits(liquidation_buffer_ratio=D("0"))
    long = size_entry(sizing(stop_price=Price(D("95.009")), mmr=D("0")), lim)
    assert long.aligned_stop == D("95.00")
    short = size_entry(
        sizing(direction=Direction.SHORT, stop_price=Price(D("104.991")), mmr=D("0")), lim
    )
    assert short.aligned_stop == D("105.00") and short.qty is not None and short.qty.value < 0


@pytest.mark.parametrize(
    "direction,stop", [(Direction.LONG, "100"), (Direction.LONG, "101"), (Direction.SHORT, "99")]
)
def test_stop_on_wrong_side_is_rejected(direction, stop):
    result = size_entry(sizing(direction=direction, stop_price=Price(D(stop))), limits())
    assert result.reason is RejectionCode.INVALID_STOP


def test_leverage_outside_limits_or_instrument_is_rejected():
    assert (
        size_entry(sizing(leverage=D("6")), limits()).reason is RejectionCode.LEVERAGE_OUT_OF_RANGE
    )
    capped = sizing(leverage=D("3"), instrument=instrument(max_lev="2"))
    assert size_entry(capped, limits()).reason is RejectionCode.LEVERAGE_OUT_OF_RANGE


def test_liquidation_approximation_matches_design_examples():
    assert approx_liquidation_price(D("100"), D("5"), D("0.005"), Direction.LONG) == D("80.500")
    assert approx_liquidation_price(D("100"), D("5"), D("0.005"), Direction.SHORT) == D("119.500")
    assert liquidation_buffer_ok(D(100), D("90.25"), D("80.5"), D("0.5"), Direction.LONG)
    assert not liquidation_buffer_ok(D(100), D("90.24"), D("80.5"), D("0.5"), Direction.LONG)
    assert liquidation_buffer_ok(D(100), D("109.75"), D("119.5"), D("0.5"), Direction.SHORT)
    assert not liquidation_buffer_ok(D(100), D("109.76"), D("119.5"), D("0.5"), Direction.SHORT)


@pytest.mark.parametrize(
    "direction,stop,ok",
    [
        (Direction.LONG, "90.25", True),
        (Direction.LONG, "90.24", False),
        (Direction.SHORT, "109.75", True),
        (Direction.SHORT, "109.76", False),
    ],
)
def test_size_entry_applies_liquidation_buffer(direction, stop, ok):
    inp = sizing(
        direction=direction, stop_price=Price(D(stop)), leverage=D("5"), equity=D("100000")
    )
    result = size_entry(inp, limits())
    assert result.ok is ok
    if not ok:
        assert result.reason is RejectionCode.LIQUIDATION_TOO_CLOSE


def test_ai_factor_outside_d15_range_is_rejected():
    for bad in ("0.49", "1.01"):
        with pytest.raises(ValueError):
            sizing(ai_factor=D(bad))
    half = size_entry(
        sizing(ai_factor=D("0.5"), mmr=D("0")), limits(liquidation_buffer_ratio=D("0"))
    )
    assert half.qty == Qty(D("8"))


# ---------------------------------------------------------------- equity (D72, D81)


def tracker_at_midnight(wallet="100", unrealized="0"):
    t = EquityTracker.open(AccountSnapshot(MIDNIGHT - timedelta(hours=1), D("100"), D("0")))
    t.start_day(AccountSnapshot(MIDNIGHT, D(wallet), D(unrealized)))
    return t


def test_baseline_is_wallet_balance_excluding_unrealized():
    carried_loss = tracker_at_midnight(unrealized="-10")
    assert carried_loss.daily_loss == D("10")
    carried_gain = tracker_at_midnight(unrealized="10")
    assert carried_gain.daily_pnl == D("10") and carried_gain.daily_loss == 0
    carried_gain.observe(AccountSnapshot(NOW, D("100"), D("0")))
    assert carried_gain.daily_pnl == 0 and carried_gain.daily_loss == 0


@pytest.mark.parametrize("amount", ["50", "-20"])
def test_cash_flows_alone_do_not_change_daily_loss(amount):
    t = tracker_at_midnight()
    flow = CashFlow("f1", NOW, D(amount))
    t.observe(AccountSnapshot(NOW, D("100") + D(amount), D("0")), [flow])
    assert t.daily_pnl == 0 and t.daily_loss == 0
    assert t.drawdown_ratio == 0


@pytest.mark.parametrize("amount", ["50", "-20"])
def test_trading_loss_survives_later_cash_flows(amount):
    t = tracker_at_midnight()
    t.observe(AccountSnapshot(NOW, D("90"), D("0")))
    later = NOW + timedelta(minutes=5)
    t.observe(
        AccountSnapshot(later, D("90") + D(amount), D("0")), [CashFlow("f", later, D(amount))]
    )
    assert t.daily_loss == D("10")
    # replaying the same flow ID is ignored
    t.observe(
        AccountSnapshot(later, D("90") + D(amount), D("0")), [CashFlow("f", later, D(amount))]
    )
    assert t.daily_loss == D("10")


def test_observe_requires_start_day_at_boundary():
    t = tracker_at_midnight()
    with pytest.raises(ValueError):
        t.observe(AccountSnapshot(MIDNIGHT + timedelta(days=1), D("100"), D("0")))
    with pytest.raises(ValueError):
        t.start_day(AccountSnapshot(NOW, D("100"), D("0")))


# ---------------------------------------------------------------- pauses


def figures(pnl="0", losses=0, last=None):
    return StrategyRiskInput(D(pnl), losses, last)


def test_l4_l5_l6_l7_create_pauses_and_timed_ones_expire():
    lim = limits()
    t = tracker_at_midnight()
    t.observe(AccountSnapshot(NOW, D("50"), D("0")))  # loss 50 = L5 limit, drawdown 50%
    pauses = evaluate_pauses(
        lim,
        t,
        {S1: figures(pnl="-30"), S2: figures(losses=3, last=NOW - timedelta(hours=1))},
        NOW,
    )
    kinds = {(p.kind, p.reason, p.strategy) for p in pauses}
    assert (PauseKind.ENTRY_PAUSED_DAILY, "L5", None) in kinds
    assert (PauseKind.HALTED, "L6", None) in kinds
    assert (PauseKind.STRATEGY_PAUSED, "L4", S1) in kinds  # 30 >= 50 * 0.6
    assert (PauseKind.STRATEGY_PAUSED, "L7", S2) in kinds
    state = PauseState(pauses)
    released = state.expire(NOW + timedelta(hours=5))  # L7 cooldown ends at NOW + 5h
    assert {p.reason for p in released} == {"L7"}
    released = state.expire(MIDNIGHT + timedelta(days=1))
    assert {p.reason for p in released} == {"L4", "L5"}
    assert state.has(PauseKind.HALTED)  # only manual resume releases HALTED


def test_l7_does_not_repause_after_cooldown_elapsed():
    pauses = evaluate_pauses(
        limits(), tracker_at_midnight(), {S2: figures(losses=5, last=NOW - timedelta(hours=7))}, NOW
    )
    assert not pauses


def test_pause_state_round_trips_through_json():
    state = PauseState(
        (
            Pause(PauseKind.HALTED, "L6", NOW),
            Pause(PauseKind.ENTRY_PAUSED_SYSTEM, "unknown-order", NOW, symbol=BTC),
            Pause(
                PauseKind.STRATEGY_PAUSED, "L7", NOW, strategy=S2, until=NOW + timedelta(hours=1)
            ),
            Pause(PauseKind.ENTRY_PAUSED_DAILY, "L5", NOW, until=MIDNIGHT + timedelta(days=1)),
        )
    )
    restored = PauseState.from_json(json.loads(json.dumps(state.to_json())))
    assert restored.active() == state.active()


def test_symbol_system_pause_blocks_only_that_contract():
    state = PauseState((Pause(PauseKind.ENTRY_PAUSED_SYSTEM, "data", NOW, symbol=BTC),))
    assert state.blocking(S1, BTC) and not state.blocking(S1, ETH)


# ---------------------------------------------------------------- limit checks


_ORDER_IDS = itertools.count(1)


def entry_order(symbol=BTC, qty="1", state=OrderState.OPEN, cum="0", purpose=OrderPurpose.ENTRY):
    n = next(_ORDER_IDS)
    link = f"d-S1-{purpose.value}-o{n}" if purpose is not OrderPurpose.ATTACHED_SL else None
    return OrderRecord(
        link,
        f"x{n}",
        None,
        S1,
        purpose,
        symbol,
        Qty(D(qty)),
        None,
        Price(D("100")),
        state,
        Qty(D("0")),
        D(cum),
        NOW,
        NOW,
    )


async def account_view(*, orders=(), positions=()):
    db = InMemoryDatabase("acct", Environment.DEMO)
    async with db.unit_of_work() as uow:
        for o in orders:
            uow.orders.put(o)
        for strategy, symbol, qty in positions:
            uow.positions.set(strategy, symbol, Qty(D(qty)))
        await uow.commit()
    async with db.unit_of_work() as uow:
        marks = {BTC: Price(D("100")), ETH: Price(D("100"))}
        return exposure(uow, marks=marks, slippage_buffer=D("0"))


def check(view, *, qty, symbol=ETH, held="0", pauses=None, lim=None):
    return check_limits(
        limits=lim or limits(max_symbol_notional_usdt=D("250"), max_account_notional_usdt=D("250")),
        view=view,
        pauses=pauses or PauseState(),
        strategy=S1,
        symbol=symbol,
        held_for_strategy=D(held),
        order_qty=Qty(D(qty)),
        notional_price=D("100"),
        leverage=D("1"),
    )


def test_unfilled_stop_does_not_offset_held_exposure():
    stop = entry_order(qty="-1", purpose=OrderPurpose.ATTACHED_SL)
    view = asyncio.run(account_view(orders=[stop], positions=[(S1, BTC, "1")]))
    assert view.total_notional == D("100")
    # 100 held + 160 new > 250 even though a -1 stop exists
    assert not check(view, qty="1.6").allowed
    assert check(view, qty="1.5").allowed


def test_unbooked_fill_counts_toward_other_contract():
    late = entry_order(qty="1", state=OrderState.CANCELLED, cum="0.4")
    view = asyncio.run(account_view(orders=[late]))
    assert view.total_notional == D("40")
    result = check(view, qty="2.2")
    assert [r.code for r in result.rejections] == [RejectionCode.L3_ACCOUNT_NOTIONAL]


def test_unknown_entry_reserves_full_quantity():
    unknown = entry_order(qty="2", state=OrderState.UNKNOWN)
    view = asyncio.run(account_view(orders=[unknown]))
    assert view.total_notional == D("200")
    assert not check(view, qty="0.6").allowed


def test_full_reduction_of_oversized_position_ignores_limits_and_pauses():
    view = asyncio.run(account_view(positions=[(S1, BTC, "20")]))
    halted = PauseState((Pause(PauseKind.HALTED, "L6", NOW),))
    result = check(view, qty="-20", symbol=BTC, held="20", pauses=halted)
    assert result.allowed and result.reducing
    crossing = check(view, qty="-21", symbol=BTC, held="20", pauses=halted)
    assert not crossing.allowed and not crossing.reducing


def test_paused_entry_reports_each_pause():
    view = asyncio.run(account_view())
    pauses = PauseState(
        (
            Pause(PauseKind.ENTRY_PAUSED_MANUAL, "manual", NOW),
            Pause(PauseKind.ENTRY_PAUSED_SYSTEM, DATA_PAUSE_REASON, NOW, symbol=ETH),
        )
    )
    result = check(view, qty="0.1", pauses=pauses)
    assert [r.code for r in result.rejections] == [RejectionCode.PAUSED, RejectionCode.PAUSED]


# ---------------------------------------------------------------- risk gate


def counter():
    values = itertools.count(1)
    return lambda: f"u{next(values)}"


def make_gate(db, *, lim=None, tracker=None, pauses=None, factory=None):
    return RiskGate(
        account_id="acct",
        environment=Environment.DEMO,
        uow_factory=factory or db.unit_of_work,
        limits=lim
        or limits(max_symbol_notional_usdt=D("1000"), max_account_notional_usdt=D("1000")),
        tracker=tracker or EquityTracker.open(AccountSnapshot(NOW, D("1000"), D("0"))),
        pauses=pauses or PauseState(),
        clock=FixedClock(NOW),
        unique_factory=counter(),
    )


def market(symbol=BTC):
    return MarketInputs(
        marks={BTC: Price(D("100")), ETH: Price(D("100"))},
        instrument=instrument(symbol),
        leverage=D("1"),
        mmr=D("0.005"),
        fee_rate=D("0"),
        slippage=D("0"),
    )


def request(symbol=BTC, signal="sig", strategy=S1, direction=Direction.LONG):
    stop = "90" if direction is Direction.LONG else "110"
    return EntryRequest(
        signal_id=signal,
        strategy=strategy,
        symbol=symbol,
        direction=direction,
        entry_price=Price(D("100")),
        stop_price=Price(D(stop)),
        ai_factor=D("1"),
    )


async def stored(db):
    async with db.unit_of_work() as uow:
        return uow.decisions.all(), uow.orders.all(), uow.episodes.all()


def test_concurrent_entries_on_two_contracts_cannot_both_pass():
    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        gate = make_gate(db)
        # each entry: allowed 80 / distance 10 = qty 8 -> notional 800; account cap 1000
        results = await asyncio.gather(
            gate.admit(request(BTC, "b"), market(BTC), {S1: figures(), S2: figures()}),
            gate.admit(request(ETH, "e"), market(ETH), {S1: figures(), S2: figures()}),
        )
        kinds = sorted(r.decision.kind for r in results)
        assert kinds == [DecisionKind.ADMITTED, DecisionKind.REJECTED]
        rejected = next(r for r in results if r.decision.kind is DecisionKind.REJECTED)
        assert RejectionCode.L3_ACCOUNT_NOTIONAL in {x.code for x in rejected.decision.rejections}
        decisions, orders, _ = await stored(db)
        assert len(decisions) == 2 and len(orders) == 1

    asyncio.run(scenario())


def test_same_signal_twice_concurrently_yields_one_decision_and_order():
    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        gate = make_gate(db)
        first, second = await asyncio.gather(
            gate.admit(request(), market(), {S1: figures(), S2: figures()}),
            gate.admit(request(), market(), {S1: figures(), S2: figures()}),
        )
        assert {first.reused, second.reused} == {False, True}
        assert first.decision == second.decision
        decisions, orders, episodes = await stored(db)
        assert len(decisions) == 1 and len(orders) == 1 and len(episodes) == 1
        (order,) = orders
        assert order.state is OrderState.CREATED and order.qty == Qty(D("8"))

    asyncio.run(scenario())


def test_commit_failure_leaves_nothing_behind():
    class FailingCommit(InMemoryUnitOfWork):
        async def commit(self):
            raise RuntimeError("database unavailable")

    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        gate = make_gate(db, factory=lambda: FailingCommit(db))
        with pytest.raises(RuntimeError):
            await gate.admit(request(), market(), {S1: figures(), S2: figures()})
        assert await stored(db) == ((), (), ())

    asyncio.run(scenario())


def test_open_episode_maintains_and_opposite_signal_is_rejected():
    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        gate = make_gate(db)
        first = await gate.admit(request(signal="day1"), market(), {S1: figures(), S2: figures()})
        assert first.episode is not None and first.episode.state is EpisodeState.OPEN
        again = await gate.admit(request(signal="day2"), market(), {S1: figures(), S2: figures()})
        assert again.decision.kind is DecisionKind.MAINTAINED and again.order is None
        flip = await gate.admit(
            request(signal="day3", direction=Direction.SHORT),
            market(),
            {S1: figures(), S2: figures()},
        )
        assert flip.decision.rejections[0].code is RejectionCode.EPISODE_OPEN_OPPOSITE
        _, orders, _ = await stored(db)
        assert len(orders) == 1

    asyncio.run(scenario())


def test_rejected_signal_is_stored_and_not_reevaluated():
    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        pauses = PauseState((Pause(PauseKind.ENTRY_PAUSED_MANUAL, "manual", NOW),))
        gate = make_gate(db, pauses=pauses)
        first = await gate.admit(request(), market(), {S1: figures(), S2: figures()})
        assert first.decision.kind is DecisionKind.REJECTED
        pauses.clear(PauseKind.ENTRY_PAUSED_MANUAL)
        second = await gate.admit(request(), market(), {S1: figures(), S2: figures()})
        assert second.reused and second.decision == first.decision

    asyncio.run(scenario())


def test_gate_refresh_adds_l5_pause_before_checking():
    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        tracker = tracker_at_midnight(wallet="1000")
        tracker.observe(AccountSnapshot(NOW, D("940"), D("0")))  # loss 60 >= 50
        gate = make_gate(db, tracker=tracker)
        result = await gate.admit(request(), market(), {S1: figures(), S2: figures()})
        assert result.decision.kind is DecisionKind.REJECTED
        assert gate.pauses.has(PauseKind.ENTRY_PAUSED_DAILY)

    asyncio.run(scenario())


def test_signal_key_is_stable_and_validated():
    key = signal_key(S2, BTC, NOW, "entry")
    assert key == "S2|BTCUSDT|2026-10-20T12:00:00+00:00|entry"
    with pytest.raises(ValueError):
        signal_key(S2, BTC, NOW, "a|b")


# ---------------------------------------------------------------- guards


def test_stale_market_data_pauses_only_that_contract_until_bars_arrive():
    hour = timedelta(hours=1)
    now = datetime(2026, 10, 20, 12, 5, tzinfo=UTC)
    ok = assess_market_data(
        BTC,
        interval=hour,
        last_closed_bar_start=now.replace(minute=0) - hour,
        now=now,
        allowed_delay=timedelta(minutes=2),
    )
    assert not ok.stale
    late = assess_market_data(
        ETH,
        interval=hour,
        last_closed_bar_start=now.replace(minute=0) - 3 * hour,
        now=now,
        allowed_delay=timedelta(minutes=2),
    )
    assert late.stale and len(late.missing_bar_starts) == 2
    state = PauseState()
    if late.stale:
        state.add(Pause(PauseKind.ENTRY_PAUSED_SYSTEM, DATA_PAUSE_REASON, now, symbol=late.symbol))
    assert state.blocking(S1, ETH) and not state.blocking(S1, BTC)
    state.clear(PauseKind.ENTRY_PAUSED_SYSTEM, reason=DATA_PAUSE_REASON, symbol=ETH)
    assert not state.blocking(S1, ETH)


def test_unregistered_actor_is_refused_and_recorded():
    auth = authorize_command("stranger", "/resume", {"owner"}, NOW)
    assert not auth.allowed and auth.actor_id == "stranger"
    assert authorize_command("owner", "/resume", {"owner"}, NOW).allowed


# ---------------------------------------------------------------- resume (D48)


def resume_setup(*pauses):
    t = EquityTracker.open(AccountSnapshot(NOW, D("100"), D("0")))
    t.observe(AccountSnapshot(NOW + timedelta(minutes=1), D("80"), D("0")))
    state = PauseState(pauses)
    ids = counter()
    service = ResumeService(
        pauses=state,
        tracker=t,
        limits=limits(),
        allowed_ids={"owner"},
        ttl=timedelta(minutes=5),
        id_factory=ids,
    )
    return t, state, service


def test_halted_resume_needs_confirmation_and_resets_only_risk_peak():
    at = NOW + timedelta(minutes=2)
    t, state, service = resume_setup(Pause(PauseKind.HALTED, "L6", NOW))
    prompt = service.request_resume("owner", at)
    assert isinstance(prompt, ResumePrompt)
    assert (prompt.record_peak, prompt.equity, prompt.drawdown_ratio) == (D(100), D(80), D("0.2"))
    assert prompt.next_halt_equity == D("72.0")
    assert state.has(PauseKind.HALTED)  # (a) not resumed without confirmation
    record = service.confirm_resume(prompt.prompt_id, "owner", at)
    assert isinstance(record, ResumeRecord) and record.cleared == (PauseKind.HALTED,)
    assert (t.risk_peak, t.record_peak) == (D(80), D(100))  # (b)
    assert not state.blocking(S1, BTC)
    t.observe(AccountSnapshot(at + timedelta(minutes=1), D("72"), D("0")))  # (c)
    assert any(p.kind is PauseKind.HALTED for p in evaluate_pauses(limits(), t, {}, at))


def test_manual_only_resume_does_not_reset_peak():
    t, state, service = resume_setup(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "manual", NOW))
    prompt = service.request_resume("owner", NOW)
    assert isinstance(prompt, ResumePrompt) and prompt.new_risk_peak is None
    record = service.confirm_resume(prompt.prompt_id, "owner", NOW)
    assert isinstance(record, ResumeRecord) and record.peak_reset is None
    assert t.risk_peak == D(100)


def test_resume_refusals():
    _, _, service = resume_setup(Pause(PauseKind.HALTED, "L6", NOW))
    assert isinstance(service.request_resume("stranger", NOW), ResumeRejected)
    prompt = service.request_resume("owner", NOW)
    assert isinstance(prompt, ResumePrompt)
    late = service.confirm_resume(prompt.prompt_id, "owner", NOW + timedelta(minutes=6))
    assert isinstance(late, ResumeRejected) and late.reason == "prompt expired"
    reused = service.confirm_resume(prompt.prompt_id, "owner", NOW)
    assert isinstance(reused, ResumeRejected) and reused.reason == "unknown prompt"
    _, _, idle = resume_setup()
    assert isinstance(idle.request_resume("owner", NOW), ResumeRejected)


# ---------------------------------------------------------------- PR #10 review regressions


def test_missing_requested_strategy_input_cannot_create_a_decision_or_order():
    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        gate = make_gate(db)
        with pytest.raises(ValueError, match="strategy risk input"):
            await gate.admit(request(), market(), {S2: figures()})
        assert await stored(db) == ((), (), ())

    asyncio.run(scenario())


@pytest.mark.parametrize("with_figures", [False, True])
def test_zero_allocation_disables_entries_even_without_strategy_figures(with_figures):
    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        gate = make_gate(db, lim=limits(strategy_allocations={S1: D(0), S2: D(1)}))
        result = await gate.admit(request(), market(), {S1: figures()} if with_figures else {})
        assert result.decision.kind is DecisionKind.REJECTED
        assert result.decision.rejections[0].code is RejectionCode.PAUSED
        assert gate.pauses.blocking(S1, BTC)[0].reason == "L4"
        _, orders, episodes = await stored(db)
        assert not orders and not episodes

    asyncio.run(scenario())


@pytest.mark.parametrize("direction,limit", [(Direction.LONG, "110"), (Direction.SHORT, "90")])
def test_limit_price_sets_sizing_and_order_reference(direction, limit):
    from dataclasses import replace

    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        gate = make_gate(db)
        req = replace(request(direction=direction), limit_price=Price(D(limit)))
        result = await gate.admit(req, market(), {S1: figures(), S2: figures()})
        assert result.decision.kind is DecisionKind.ADMITTED
        assert result.order.reference_price == req.limit_price
        assert result.order.qty.value.copy_abs() == D(4)
        basis = dict(result.decision.basis)
        actual_loss = (
            result.order.qty.value.copy_abs() * (D(limit) - D(basis["aligned_stop"])).copy_abs()
        )
        assert actual_loss <= D(basis["allowed_loss"]) == D(80)
        assert D(basis["entry_price"]) == D(limit)

    asyncio.run(scenario())


@pytest.mark.parametrize("direction,limit", [(Direction.LONG, "80"), (Direction.SHORT, "120")])
def test_limit_price_stop_wrong_side_is_rejected(direction, limit):
    from dataclasses import replace

    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        result = await make_gate(db).admit(
            replace(request(direction=direction), limit_price=Price(D(limit))),
            market(),
            {S1: figures(), S2: figures()},
        )
        assert result.decision.rejections[0].code is RejectionCode.INVALID_STOP
        assert result.order is None

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["account", "strategy"])
def test_uow_wait_rechecks_risk_inputs_before_admission(change):
    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        tracker = EquityTracker.open(AccountSnapshot(NOW, D(1000), D(0)))
        gate = make_gate(db, tracker=tracker)
        current = {S1: figures(), S2: figures()}
        async with db.unit_of_work():
            task = asyncio.create_task(gate.admit(request(), market(), current))
            await asyncio.sleep(0)
            assert not task.done()
            if change == "account":
                tracker.observe(AccountSnapshot(NOW, D(940), D(0)))
            else:
                current[S1] = figures(pnl="-30")
        result = await task
        assert result.decision.kind is DecisionKind.REJECTED
        assert any(
            p.reason == ("L5" if change == "account" else "L4") for p in gate.pauses.active()
        )
        _, orders, episodes = await stored(db)
        assert not orders and not episodes

    asyncio.run(scenario())


@pytest.mark.parametrize("wallet", ["70", "90"])
def test_resume_equity_change_requires_new_confirmation(wallet):
    at = NOW + timedelta(minutes=2)
    t, state, svc = resume_setup(Pause(PauseKind.HALTED, "L6", NOW))
    old = svc.request_resume("owner", at)
    later = at + timedelta(seconds=30)
    t.observe(AccountSnapshot(later, D(wallet), D(0)))
    result = svc.confirm_resume(old.prompt_id, "owner", later)
    assert isinstance(result, ResumeRejected) and "state changed" in result.reason
    assert state.has(PauseKind.HALTED) and t.risk_peak == D(100)
    fresh = svc.request_resume("owner", later)
    assert fresh.new_risk_peak == D(wallet)
    result = svc.confirm_resume(fresh.prompt_id, "owner", later)
    assert isinstance(result, ResumeRecord)
    assert t.risk_peak == D(wallet) and not state.has(PauseKind.HALTED)


@pytest.mark.parametrize("kind", [PauseKind.ENTRY_PAUSED_MANUAL, PauseKind.HALTED])
def test_old_parallel_resume_prompt_cannot_clear_a_new_stop(kind):
    at = NOW + timedelta(minutes=2)
    _, state, svc = resume_setup(Pause(kind, "original", NOW))
    first = svc.request_resume("owner", at)
    second = svc.request_resume("owner", at)
    assert isinstance(svc.confirm_resume(first.prompt_id, "owner", at), ResumeRecord)
    state.add(Pause(kind, "new incident", at + timedelta(seconds=1)))
    assert isinstance(
        svc.confirm_resume(second.prompt_id, "owner", at + timedelta(seconds=2)), ResumeRejected
    )
    assert state.has(kind)


@pytest.mark.parametrize("kind", [PauseKind.ENTRY_PAUSED_MANUAL, PauseKind.HALTED])
def test_resume_detects_same_pause_key_removed_and_readded(kind):
    at = NOW + timedelta(minutes=2)
    _, state, svc = resume_setup(Pause(kind, "same reason", NOW))
    prompt = svc.request_resume("owner", at)
    state.clear(kind)
    state.add(Pause(kind, "same reason", NOW))
    result = svc.confirm_resume(prompt.prompt_id, "owner", at)
    assert isinstance(result, ResumeRejected) and "state changed" in result.reason
    assert state.has(kind)


def test_nonpositive_equity_cannot_clear_halted():
    at = NOW + timedelta(minutes=2)
    t, state, svc = resume_setup(Pause(PauseKind.HALTED, "L6", NOW))
    t.observe(AccountSnapshot(at, D(0), D(0)))
    result = svc.request_resume("owner", at)
    assert isinstance(result, ResumeRejected) and state.has(PauseKind.HALTED)


@pytest.mark.parametrize("amount", ["50", "-20"])
@pytest.mark.parametrize("loss", ["0", "10"])
def test_start_day_cash_flow_adjusts_peaks_without_double_counting(amount, loss):
    t = EquityTracker.open(AccountSnapshot(MIDNIGHT, D(100), D(0)))
    before = MIDNIGHT + timedelta(hours=23)
    t.observe(AccountSnapshot(before, D(100) - D(loss), D(0)))
    boundary = MIDNIGHT + timedelta(days=1)
    flow = CashFlow("boundary-flow", boundary, D(amount))
    snapshot = AccountSnapshot(boundary, D(100) - D(loss) + D(amount), D(0))
    t.start_day(snapshot, [flow, flow])
    assert t.risk_peak == D(100) + D(amount)
    assert t.record_peak == D(100) + D(amount)
    assert t.daily_loss == 0 and t.state.net_deposit == 0
    assert t.drawdown_ratio == D(loss) / (D(100) + D(amount))
    saved = t.state
    t.observe(snapshot, [flow])
    assert t.state == saved
    later = boundary + timedelta(hours=1)
    t.observe(AccountSnapshot(later, snapshot.wallet_balance - D(5), D(0)), [flow])
    assert t.daily_loss == D(5)


def test_start_day_cash_flow_validation_is_atomic():
    t = tracker_at_midnight()
    before = t.state
    boundary = MIDNIGHT + timedelta(days=1)
    with pytest.raises(ValueError, match="later than"):
        t.start_day(
            AccountSnapshot(boundary, D(150), D(0)),
            [
                CashFlow("ok", boundary, D(50)),
                CashFlow("future", boundary + timedelta(seconds=1), D(1)),
            ],
        )
    assert t.state == before
