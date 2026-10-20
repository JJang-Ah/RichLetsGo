"""M1-04 offline acceptance scenarios, including races and restart boundaries."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_oms import order as oms_order
from test_risk import BTC, NOW, S1, S2, counter, figures, limits, market

from richletsgo.domain import (
    Environment,
    ExecutionEvent,
    ManualClock,
    OrderEvent,
    OrderPurpose,
    OrderState,
    OrderType,
    Price,
    Qty,
    StopKind,
    TimeInForce,
)
from richletsgo.oms.fills import FillResult, apply_execution
from richletsgo.portfolio.closing import CloseView, StrategyView, advance_close
from richletsgo.portfolio.models import (
    ActionKind,
    CloseIntent,
    ClosePolicy,
    CloseState,
    StopLink,
    StopParameters,
    StrategyBook,
    apply_book_fill,
    entry_delta,
)
from richletsgo.portfolio.service import PortfolioService, invariants
from richletsgo.portfolio.signals import (
    AiMode,
    AiScore,
    AiScoreStore,
    MarketSignal,
    ScoreStatus,
    SignalRouter,
)
from richletsgo.risk.equity import AccountSnapshot, EquityTracker
from richletsgo.risk.gate import EntryRequest, RiskGate
from richletsgo.risk.pause import Pause, PauseKind, PauseState
from richletsgo.risk.records import DecisionKind, Direction, EpisodeState
from richletsgo.storage.memory import InMemoryDatabase

POLICY = ClosePolicy(2, 2, timedelta(seconds=5), "LastPrice")
RISK_INPUT = {S1: figures(), S2: figures()}


def setup(account="acct", scores=None, mode=AiMode.DISABLED, pauses=None):
    db = InMemoryDatabase(account, Environment.DEMO)
    clock, ids = ManualClock(NOW), counter()
    svc = PortfolioService(db.unit_of_work, clock, ids, POLICY)
    gate = RiskGate(
        account_id=account,
        environment=Environment.DEMO,
        uow_factory=db.unit_of_work,
        limits=limits(risk_per_trade=D("0.01"), max_account_notional_usdt=D(10000)),
        tracker=EquityTracker.open(AccountSnapshot(NOW, D(1000), D(0))),
        pauses=pauses or PauseState(),
        clock=clock,
        unique_factory=ids,
    )
    router = SignalRouter(
        gate,
        svc,
        scores or AiScoreStore(),
        mode=mode,
        threshold=D("0.5"),
        factor=lambda s: s,
        trigger_by="LastPrice",
    )
    return db, clock, svc, router


def signal(strategy=S1, direction=Direction.LONG, bar=NOW, kind="ENTRY"):
    return MarketSignal(
        strategy,
        BTC,
        bar,
        kind,
        direction,
        Price(D(100)),
        Price(D(90 if direction is Direction.LONG else 110)),
    )


def fill(order, qty, *, key="exec", price="100", fee="0.1"):
    return ExecutionEvent(
        key,
        order.order_link_id,
        order.exchange_order_id or "ex-" + order.key,
        BTC,
        Qty(D(qty)),
        Price(D(price)),
        D(fee),
        NOW,
        False,
    )


async def orders(db):
    async with db.unit_of_work() as u:
        return u.orders.all()


async def terminal(db, order, state=OrderState.FILLED, cum=None):
    async with db.unit_of_work() as u:
        o = u.orders.get(order.key)
        u.orders.put(
            replace(
                o,
                state=state,
                exchange_cum_exec_qty=D(cum) if cum is not None else abs(o.booked_qty.value),
            )
        )
        await u.commit()


def test_same_signal_atomic_and_ai_half_does_not_top_up():
    async def scenario():
        scores = AiScoreStore()
        sig = signal()
        scores.put(sig.signal_id, AiScore(ScoreStatus.OK, D("0.5")))
        db, clock, svc, router = setup(scores=scores, mode=AiMode.ENABLED)
        a, b = await asyncio.gather(*(router.handle(sig, market(), RISK_INPUT) for _ in range(2)))
        assert a.decision == b.decision and len(await orders(db)) == 1
        assert a.decision.original_qty == Qty(D(1)) and a.decision.approved_qty == Qty(D("0.5"))
        assert a.decision.ai_score == D("0.5")
        await svc.on_execution(fill(a.order, "0.2"))
        await terminal(db, a.order, OrderState.CANCELLED)
        nxt = signal(bar=NOW + timedelta(hours=1))
        scores.put(nxt.signal_id, AiScore(ScoreStatus.OK, D(1)))
        result = await router.handle(nxt, market(), RISK_INPUT)
        assert result.decision.kind is DecisionKind.MAINTAINED and result.order is None
        assert len([o for o in await orders(db) if o.purpose is OrderPurpose.ENTRY]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("status", list(ScoreStatus)[1:] + [None])
@pytest.mark.parametrize("mode", list(AiMode))
def test_score_failure_and_missing_score_modes(status, mode):
    async def scenario():
        scores = AiScoreStore()
        sig = signal()
        if status:
            scores.put(sig.signal_id, AiScore(status))
        db, _, _, router = setup(scores=scores, mode=mode)
        a = await router.handle(sig, market(), RISK_INPUT)
        assert (a.order is not None) == (mode is AiMode.DISABLED)
        assert a.decision.kind is (
            DecisionKind.ADMITTED if mode is AiMode.DISABLED else DecisionKind.REJECTED
        )
        assert len(await orders(db)) == (1 if mode is AiMode.DISABLED else 0)

    asyncio.run(scenario())


def test_shared_scores_isolated_halt_and_noai():
    async def scenario():
        scores = AiScoreStore()
        sig = signal()
        scores.put(sig.signal_id, AiScore(ScoreStatus.OK, D(1)))
        halted = PauseState((Pause(PauseKind.HALTED, "test", NOW),))
        live = setup("real", scores, AiMode.ENABLED, halted)
        shadow = setup("shadow_ai", scores, AiMode.ENABLED)
        noai = setup("shadow_noai", scores, AiMode.DISABLED)
        results = [await x[3].handle(sig, market(), RISK_INPUT) for x in (live, shadow, noai)]
        assert [r.order is not None for r in results] == [False, True, True]
        missing = signal(bar=NOW + timedelta(hours=1), strategy=S2)
        results = [await x[3].handle(missing, market(), RISK_INPUT) for x in (live, shadow, noai)]
        assert [r.order is not None for r in results] == [False, False, True]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "held,target,reserved,unbooked,expected",
    [
        ("0", "-1", "0", "0", "-1"),
        ("-1", "-1.5", "0", "0", "-0.5"),
        ("-1.5", "-1", "0", "0", "0"),
        ("1", "-1", "0", "0", "0"),
        ("0", "1", "0", "0", "1"),
        ("0.4", "1", "0.3", "0.2", "0.1"),
        ("-0.4", "-1", "-0.3", "-0.2", "-0.1"),
    ],
)
def test_signed_delta_table(held, target, reserved, unbooked, expected):
    assert entry_delta(Qty(D(target)), Qty(D(held)), Qty(D(reserved)), Qty(D(unbooked))) == Qty(
        D(expected)
    )


def test_entry_requires_stop_and_payload_uses_aligned_stop():
    with pytest.raises(TypeError):
        StopParameters(None, "LastPrice")
    with pytest.raises(TypeError):
        EntryRequest("sig", S1, BTC, Direction.LONG, Price(D(100)), None, D(1))

    async def scenario():
        _, _, _, r = setup()
        a = await r.handle(signal(), market(), RISK_INPUT)
        p = a.order.request_payload()
        assert (p["stopLoss"], p["tpslMode"], p["slOrderType"], p["slTriggerBy"]) == (
            "90",
            "Partial",
            "Market",
            "LastPrice",
        )
        with pytest.raises(ValueError):
            replace(a.order, attached_stop=None).request_payload()

    asyncio.run(scenario())


def test_never_filled_expired_entry_closes_episode():
    async def scenario():
        db, _, svc, r = setup()
        a = await r.handle(signal(), market(), RISK_INPUT)
        await terminal(db, a.order, OrderState.EXPIRED)
        await svc.tick()
        b = await r.handle(signal(bar=NOW + timedelta(hours=1)), market(), RISK_INPUT)
        assert b.order and b.episode.episode_id != a.episode.episode_id

    asyncio.run(scenario())


def test_exit_signal_creates_close_even_when_ai_missing_and_halted():
    async def scenario():
        db, _, svc, r = setup()
        a = await r.handle(signal(), market(), RISK_INPUT)
        await svc.on_execution(fill(a.order, "1"))
        await terminal(db, a.order)
        r.mode = AiMode.ENABLED
        r.gate.pauses.add(Pause(PauseKind.HALTED, "halt", NOW))
        result = await r.handle(
            signal(direction=Direction.SHORT, bar=NOW + timedelta(hours=1)), market(), {}
        )
        assert result.order is None
        async with db.unit_of_work() as u:
            assert u.episodes.get(a.episode.episode_id).state is EpisodeState.EXITING
            assert u.portfolio.intent(a.episode.episode_id)
            close = next(o for o in u.orders.all() if o.purpose is OrderPurpose.CLOSE)
            assert close.qty == Qty(D(-1)) and close.request_payload()["reduceOnly"]

    asyncio.run(scenario())


@pytest.mark.parametrize("direction", [1, -1])
def test_average_pnl_fees_and_funding_are_exact(direction):
    b = StrategyBook(S1, BTC)
    b = apply_book_fill(b, D(direction), D(100), D("0.1"))
    b = apply_book_fill(b, D(direction), D(120), D("0.1"))
    assert b.avg_price == D(110)
    b = apply_book_fill(b, D(-direction), D(130), D("0.1"))
    assert b.realized_pnl == D(20 * direction) and b.fees == D("0.3")

    async def scenario():
        db, _, svc, _ = setup()
        async with db.unit_of_work() as u:
            u.portfolio.put_book(b)
            u.portfolio.put_book(StrategyBook(S2, BTC, D(direction), D(110)))
            await u.commit()
        await svc.funding("f", BTC, D("0.3"))
        await svc.funding("f", BTC, D("0.3"))
        async with db.unit_of_work() as u:
            assert u.portfolio.book(S1, BTC).funding == D("0.15")
            assert u.portfolio.book(S2, BTC).funding == D("0.15")

    asyncio.run(scenario())


def test_fill_hook_rolls_back_books_actions_and_dedup_on_error():
    async def scenario():
        db, _, svc, r = setup()
        a = await r.handle(signal(), market(), RISK_INPUT)
        event = fill(a.order, "1")

        def fail(u, e, attr):
            svc.book_execution(u, e, attr)
            raise RuntimeError("abort")

        with pytest.raises(RuntimeError):
            await apply_execution(db.unit_of_work(), event, fail)
        async with db.unit_of_work() as u:
            assert not u.portfolio.books() and not u.portfolio.actions() and not u.fills.all()
        assert await svc.on_execution(event) is FillResult.BOOKED
        assert await svc.on_execution(event) is FillResult.DUPLICATE
        async with db.unit_of_work() as u:
            assert u.portfolio.book(S1, BTC).qty == 1

    asyncio.run(scenario())


def views(
    *,
    held="1",
    other="0",
    covered="0",
    pending=False,
    outstanding="0",
    entries=(),
    settled=True,
    revision=0,
    ownerstops=("sl1",),
    pendingfills=False,
):
    return CloseView(
        (
            StrategyView(S1, D(held), D(1), False, D(outstanding), ownerstops),
            StrategyView(S2, D(other), D(covered), pending, D(0), ("sl2",) if D(covered) else ()),
        ),
        entries,
        settled,
        pendingfills,
        revision,
    )


def ci(state=CloseState.RECORDED):
    return CloseIntent("intent", "episode", S1, BTC, NOW, state)


def test_close_ioc_retry_exhaustion_and_restart_keeps_stops():
    intent, actions = advance_close(ci(), views(), NOW, POLICY)
    assert [a.kind for a in actions] == [ActionKind.CLOSE]
    restored = CloseIntent.from_dict(json.loads(json.dumps(intent.to_dict())))
    assert restored == intent
    intent, actions = advance_close(
        restored, views(held="0.6", outstanding="0.6"), NOW + timedelta(seconds=5), POLICY
    )
    assert not actions
    intent, actions = advance_close(intent, views(held="0.6"), NOW + timedelta(seconds=6), POLICY)
    assert [a.qty for a in actions if a.kind is ActionKind.CLOSE] == [D("-0.6")]
    intent, actions = advance_close(intent, views(held="0.2"), NOW + timedelta(seconds=12), POLICY)
    assert {a.kind for a in actions} == {ActionKind.PAUSE, ActionKind.ALERT}


@pytest.mark.parametrize(
    "v",
    [
        views(),
        views(held="0", settled=False, entries=("entry",)),
        views(held="0", pendingfills=True),
        views(held="0", outstanding="0.1"),
    ],
)
def test_no_stop_cancellation_before_flat_confirmation(v):
    intent, actions = advance_close(ci(CloseState.STOPS_CANCELLING), v, NOW, POLICY)
    assert intent.state is CloseState.CLOSING
    assert all(a.kind is not ActionKind.CANCEL_STOP for a in actions)


def test_other_protection_confirmed_before_owner_stop_cancel_and_final_requery():
    intent, actions = advance_close(ci(), views(held="0", other="1", covered="0.4"), NOW, POLICY)
    assert intent.state is CloseState.FLAT_CONFIRMED
    intent, _ = advance_close(intent, views(held="0", other="1", covered="0.4"), NOW, POLICY)
    intent, actions = advance_close(
        intent, views(held="0", other="1", covered="0.4", revision=1), NOW, POLICY
    )
    assert next(a for a in actions if a.kind is ActionKind.PROTECT).qty == D("-0.6")
    assert all(a.kind is not ActionKind.CANCEL_STOP for a in actions)
    intent, actions = advance_close(
        intent, views(held="0", other="1", covered="0.4", pending=True, revision=2), NOW, POLICY
    )
    assert intent.state is CloseState.PROTECTING_OTHERS
    intent, _ = advance_close(
        intent, views(held="0", other="1", covered="1", revision=3), NOW, POLICY
    )
    assert intent.state is CloseState.STOPS_CANCELLING
    intent, actions = advance_close(
        intent, views(held="0", other="1", covered="1", revision=3), NOW, POLICY
    )
    assert [(a.kind, a.order_key) for a in actions if a.kind is ActionKind.CANCEL_STOP] == [
        (ActionKind.CANCEL_STOP, "sl1")
    ]
    same, _ = advance_close(
        intent, views(held="0", other="1", covered="1", revision=3, ownerstops=()), NOW, POLICY
    )
    assert same.state is CloseState.VERIFYING
    done, _ = advance_close(
        intent, views(held="0", other="1", covered="1", revision=4, ownerstops=()), NOW, POLICY
    )
    assert done.state is CloseState.DONE


def test_failed_other_protection_reduces_uncovered_without_cancelling_owner():
    intent = replace(ci(CloseState.PROTECTING_OTHERS), protect_attempts=((S2, 2),))
    intent, actions = advance_close(
        intent, views(held="0", other="1", covered="0.4", revision=1), NOW, POLICY
    )
    assert next(a for a in actions if a.kind is ActionKind.REDUCE_UNPROTECTED).qty == D("-0.6")
    assert not any(a.kind is ActionKind.CANCEL_STOP for a in actions)
    assert intent.state is CloseState.PROTECTING_OTHERS


def test_late_entry_fill_restarts_closing_and_cancels_remaining_entry():
    intent, actions = advance_close(
        ci(CloseState.VERIFYING), views(held="0.6", entries=("e",), settled=False), NOW, POLICY
    )
    assert intent.state is CloseState.CLOSING
    assert {a.kind for a in actions} == {ActionKind.CANCEL_ENTRY, ActionKind.CLOSE}


def test_book_invariants_detect_missing_protection_opposing_books_and_exchange_mismatch():
    books = [StrategyBook(S1, BTC, D(1), D(100), stops=(StopLink("a", D(1)),))]
    assert not invariants(books, {BTC: Qty(D(1))})
    assert invariants(books, {BTC: Qty(D(2))})
    assert invariants([replace(books[0], stops=())], {BTC: Qty(D(1))})
    assert not invariants([replace(books[0], stops=())], {BTC: Qty(D(1))}, closing=frozenset({BTC}))


def test_external_confirmed_reduction_allocates_s2_first_and_pending_does_not():
    async def scenario():
        db, _, svc, _ = setup()
        async with db.unit_of_work() as u:
            for strategy in (S1, S2):
                u.orders.put(
                    replace(
                        oms_order(
                            strategy=strategy,
                            purpose=OrderPurpose.PROTECT,
                            qty="-1",
                            state=OrderState.OPEN,
                            suffix=strategy.value,
                        ),
                        trigger_price=Price(D(90)),
                    )
                )
                u.portfolio.put_book(
                    StrategyBook(strategy, BTC, D(1), D(100), stop_price=Price(D(90)))
                )
            await u.commit()
        e = ExecutionEvent(
            "foreign", None, "external", BTC, Qty(D("-1.5")), Price(D(110)), D("0.3"), NOW, False
        )
        assert await svc.on_execution(e) is FillResult.PENDING_ATTRIBUTION
        async with db.unit_of_work() as u:
            assert u.portfolio.book(S2, BTC).qty == 1
        event = OrderEvent(
            None,
            "external",
            None,
            OrderState.FILLED,
            D("1.5"),
            NOW,
            {},
            BTC,
            Qty(D("-1.5")),
            OrderType.MARKET,
            TimeInForce.IOC,
            None,
            None,
            True,
            None,
        )
        await svc.on_order(event, reference_price=Price(D(110)), lookup_confirmed=True)
        async with db.unit_of_work() as u:
            assert u.portfolio.book(S2, BTC).qty == 0 and u.portfolio.book(S1, BTC).qty == D("0.5")
            assert u.portfolio.book(S2, BTC).realized_pnl == 10
            assert u.portfolio.book(S1, BTC).realized_pnl == 5
            assert u.portfolio.paused(BTC)
            assert sum((b.fees for b in u.portfolio.books()), D(0)) == D("0.3")
            assert any(a.kind is ActionKind.TRIM_STOP for a in u.portfolio.actions())

    asyncio.run(scenario())


def test_opposite_other_strategy_reservation_blocks_entry():
    async def scenario():
        _, _, _, r = setup()
        await r.handle(signal(), market(), RISK_INPUT)
        result = await r.handle(signal(S2, Direction.SHORT), market(), RISK_INPUT)
        assert result.decision.kind is DecisionKind.REJECTED

    asyncio.run(scenario())


def attached_event(parent, qty="-1", stop_id="sl"):
    return OrderEvent(
        None,
        stop_id,
        parent.order_link_id,
        OrderState.OPEN,
        D(0),
        NOW,
        {},
        BTC,
        Qty(D(qty)),
        OrderType.MARKET,
        TimeInForce.IOC,
        None,
        Price(D(90)),
        True,
        StopKind.PARTIAL_STOP_LOSS,
    )


def test_attached_stop_parent_attribution_and_no_duplicate_protection():
    async def scenario():
        db, _, svc, r = setup()
        a = await r.handle(signal(), market(), RISK_INPUT)
        await svc.on_order(attached_event(a.order, qty="-0.4"), reference_price=Price(D(90)))
        await svc.on_execution(fill(a.order, "0.4"))
        async with db.unit_of_work() as u:
            b = u.portfolio.book(S1, BTC)
            assert b.qty == D("0.4") and sum((s.qty for s in b.stops), D(0)) == D("0.4")
            assert not any(o.purpose is OrderPurpose.PROTECT for o in u.orders.all())
            assert u.portfolio.book(S2, BTC).qty == 0

    asyncio.run(scenario())


def test_partial_stop_then_unknown_entry_late_fill_is_closed_not_reentered():
    async def scenario():
        db, clock, svc, r = setup()
        a = await r.handle(signal(), market(), RISK_INPUT)
        await svc.on_order(attached_event(a.order, qty="-0.4"), reference_price=Price(D(90)))
        await svc.on_execution(fill(a.order, "0.4", key="first"))
        # Execution comes through its exchange ID; strategy is resolved via parent order.
        stopfill = ExecutionEvent(
            "stopfill", None, "sl", BTC, Qty(D("-0.4")), Price(D(90)), D(0), NOW, False
        )
        await svc.on_execution(stopfill)
        await terminal(db, a.order, OrderState.UNKNOWN, cum="0.4")
        await svc.tick()
        b = await r.handle(signal(bar=NOW + timedelta(hours=1)), market(), RISK_INPUT)
        assert b.order is None
        async with db.unit_of_work() as u:
            assert u.episodes.get(a.episode.episode_id).state is EpisodeState.EXITING
            assert any(x.kind is ActionKind.CANCEL_ENTRY for x in u.portfolio.actions())
        clock.advance(timedelta(seconds=6))
        await svc.on_execution(fill(a.order, "0.6", key="late"))
        async with db.unit_of_work() as u:
            assert u.portfolio.book(S1, BTC).qty == D("0.6")
            closes = [o for o in u.orders.all() if o.purpose is OrderPurpose.CLOSE]
            assert len(closes) == 1 and closes[0].qty == Qty(D("-0.6"))
            assert all(x.kind is not ActionKind.CANCEL_STOP for x in u.portfolio.actions())
        await svc.tick()
        assert len([o for o in await orders(db) if o.purpose is OrderPurpose.CLOSE]) == 1

    asyncio.run(scenario())


def test_stop_execution_before_parent_event_is_pending_then_booked_once():
    async def scenario():
        db, _, svc, r = setup()
        a = await r.handle(signal(), market(), RISK_INPUT)
        await svc.on_execution(fill(a.order, "1"))
        e = ExecutionEvent(
            "early-stop", None, "early-sl", BTC, Qty(D(-1)), Price(D(90)), D(0), NOW, False
        )
        assert await svc.on_execution(e) is FillResult.PENDING_ATTRIBUTION
        async with db.unit_of_work() as u:
            assert u.portfolio.book(S1, BTC).qty == 1
        await svc.on_order(
            attached_event(a.order, stop_id="early-sl"), reference_price=Price(D(90))
        )
        async with db.unit_of_work() as u:
            assert u.portfolio.book(S1, BTC).qty == 0
            assert u.portfolio.book(S1, BTC).realized_pnl == D(-10)
        assert await svc.on_execution(e) is FillResult.DUPLICATE

    asyncio.run(scenario())


def test_stop_flat_terminal_then_cleanup_allows_new_episode():
    async def scenario():
        db, clock, svc, r = setup()
        a = await r.handle(signal(), market(), RISK_INPUT)
        await svc.on_order(attached_event(a.order), reference_price=Price(D(90)))
        await svc.on_execution(fill(a.order, "1"))
        await terminal(db, a.order)
        await svc.on_execution(
            ExecutionEvent("stop", None, "sl", BTC, Qty(D(-1)), Price(D(90)), D(0), NOW, False)
        )
        clock.advance(timedelta(seconds=1))
        await svc.tick()
        await svc.confirm_stop_snapshot(BTC, (), clock.now())
        await svc.tick()
        clock.advance(timedelta(seconds=1))
        await svc.confirm_stop_snapshot(BTC, (), clock.now())
        async with db.unit_of_work() as u:
            assert u.portfolio.intent(a.episode.episode_id).state is CloseState.DONE
            assert u.episodes.get(a.episode.episode_id).state is EpisodeState.CLOSED
        b = await r.handle(signal(bar=NOW + timedelta(hours=1)), market(), RISK_INPUT)
        assert b.order and b.episode.episode_id != a.episode.episode_id

    asyncio.run(scenario())


def test_attributed_overflow_uses_s2_then_s1_without_external_pause():
    async def scenario():
        db, _, svc, _ = setup()
        close = oms_order(purpose=OrderPurpose.CLOSE, qty="-1.5", state=OrderState.OPEN)
        async with db.unit_of_work() as u:
            u.orders.put(close)
            for s in (S1, S2):
                u.portfolio.put_book(StrategyBook(s, BTC, D(1), D(100), stop_price=Price(D(90))))
                u.orders.put(
                    replace(
                        oms_order(
                            strategy=s,
                            purpose=OrderPurpose.PROTECT,
                            qty="-1",
                            state=OrderState.OPEN,
                            suffix=s.value,
                        ),
                        trigger_price=Price(D(90)),
                    )
                )
            await u.commit()
        await svc.on_execution(fill(close, "-1.5"))
        async with db.unit_of_work() as u:
            assert u.portfolio.book(S1, BTC).qty == 0 and u.portfolio.book(S2, BTC).qty == D("0.5")
            assert not u.portfolio.paused(BTC)
            assert any(x.kind is ActionKind.ALERT for x in u.portfolio.actions())
            assert any(
                x.kind is ActionKind.TRIM_STOP and x.strategy is S2 for x in u.portfolio.actions()
            )

    asyncio.run(scenario())


def test_unconfirmed_protect_times_out_to_reduce_without_duplicate_protect():
    intent = replace(
        ci(CloseState.PROTECTING_OTHERS), protect_attempts=((S2, 1),), snapshot_revision=0
    )
    v = views(held="0", other="1", covered="0.4", pending=True, revision=1)
    intent, actions = advance_close(intent, v, NOW, POLICY)
    assert not any(a.kind is ActionKind.PROTECT for a in actions)
    intent, actions = advance_close(intent, v, NOW + timedelta(seconds=6), POLICY)
    assert any(a.kind is ActionKind.REDUCE_UNPROTECTED for a in actions)
    assert not any(a.kind is ActionKind.CANCEL_STOP for a in actions)


def test_score_is_immutable_and_threshold_or_disagreement_block():
    async def scenario():
        for score in (AiScore(ScoreStatus.OK, D("0.4")), AiScore(ScoreStatus.OK, D(1), False)):
            store = AiScoreStore()
            sig = signal()
            store.put(sig.signal_id, score)
            with pytest.raises(ValueError):
                store.put(sig.signal_id, AiScore(ScoreStatus.OK, D("0.8")))
            _, _, _, r = setup(scores=store, mode=AiMode.ENABLED)
            a = await r.handle(sig, market(), RISK_INPUT)
            assert a.order is None and a.decision.original_qty == Qty(D(1))

    asyncio.run(scenario())


def test_missing_attachment_requests_only_missing_protection_and_not_twice():
    async def scenario():
        db, _, svc, r = setup()
        a = await r.handle(signal(), market(), RISK_INPUT)
        await svc.on_order(attached_event(a.order, qty="-0.2"), reference_price=Price(D(90)))
        await svc.on_execution(fill(a.order, "0.4"))
        await svc.tick()
        protect = [o for o in await orders(db) if o.purpose is OrderPurpose.PROTECT]
        assert len(protect) == 1 and protect[0].qty == Qty(D("-0.2"))
        payload = protect[0].request_payload()
        assert payload["reduceOnly"] and payload["triggerBy"] == "LastPrice"
        assert payload["triggerPrice"] == "90" and payload["triggerDirection"] == "2"

    asyncio.run(scenario())


def test_restart_service_reuses_open_close_order_then_retries_ioc_remainder():
    async def scenario():
        db, clock, svc, r = setup()
        a = await r.handle(signal(), market(), RISK_INPUT)
        await svc.on_order(attached_event(a.order), reference_price=Price(D(90)))
        await svc.on_execution(fill(a.order, "1"))
        await terminal(db, a.order)
        await r.handle(signal(kind="EXIT", bar=NOW + timedelta(hours=1)), market(), RISK_INPUT)
        close = next(o for o in await orders(db) if o.purpose is OrderPurpose.CLOSE)
        # Fresh orchestrator; durable IDs are supplied by caller across restarts.
        restarted = PortfolioService(
            db.unit_of_work, clock, iter(["restart1", "restart2"]).__next__, POLICY
        )
        await restarted.tick()
        assert len([o for o in await orders(db) if o.purpose is OrderPurpose.CLOSE]) == 1
        await restarted.on_execution(fill(close, "-0.4", key="ioc"))
        await terminal(db, close, OrderState.CANCELLED)
        clock.advance(timedelta(seconds=6))
        await restarted.tick()
        closes = [o for o in await orders(db) if o.purpose is OrderPurpose.CLOSE]
        assert len(closes) == 2 and closes[-1].qty == Qty(D("-0.6"))
        async with db.unit_of_work() as u:
            assert not any(x.kind is ActionKind.CANCEL_STOP for x in u.portfolio.actions())

    asyncio.run(scenario())


def test_normal_strategy_close_does_not_change_other_strategy_book_or_stops():
    async def scenario():
        db, _, svc, r = setup()
        a = await r.handle(signal(), market(), RISK_INPUT)
        b = await r.handle(signal(S2), market(), RISK_INPUT)
        for admission, key in [(a, "stop1"), (b, "stop2")]:
            await svc.on_order(
                attached_event(admission.order, stop_id=key), reference_price=Price(D(90))
            )
            await svc.on_execution(fill(admission.order, "1", key=key + "entry"))
            await terminal(db, admission.order)
        async with db.unit_of_work() as u:
            before = u.portfolio.book(S2, BTC)
        await r.handle(signal(kind="EXIT", bar=NOW + timedelta(hours=1)), market(), RISK_INPUT)
        close = next(o for o in await orders(db) if o.purpose is OrderPurpose.CLOSE)
        await svc.on_execution(fill(close, "-1", key="close-s1"))
        async with db.unit_of_work() as u:
            assert u.portfolio.book(S2, BTC) == before
            assert not any(
                x.kind in (ActionKind.CANCEL_STOP, ActionKind.TRIM_STOP) and x.strategy is S2
                for x in u.portfolio.actions()
            )

    asyncio.run(scenario())


def test_decision_hook_failure_rolls_back_entry_decision_episode_and_close_intent():
    async def scenario():
        db, _, _, r = setup()

        def fail(u, a):
            raise RuntimeError("before commit")

        request = EntryRequest("atomic", S1, BTC, Direction.LONG, Price(D(100)), Price(D(90)), D(1))
        with pytest.raises(RuntimeError):
            await r.gate.admit(request, market(), RISK_INPUT, after_decision=fail)
        async with db.unit_of_work() as u:
            assert not u.orders.all() and not u.decisions.all() and not u.episodes.all()
            assert not u.portfolio.intents() and not u.portfolio.actions()

    asyncio.run(scenario())
