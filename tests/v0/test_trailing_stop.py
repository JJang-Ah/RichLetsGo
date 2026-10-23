"""M2-02a: real portfolio, journal, coordinator and simulated venue integration."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_portfolio import RISK_INPUT, signal
from test_recovery_integration import POLICY, bar, book, setup
from test_risk import BTC, NOW, market

from richletsgo.domain import OrderPurpose, OrderState, Price, StrategyId, Symbol
from richletsgo.exchange.contracts import Accepted, ErrorCode, ExchangeError
from richletsgo.exchange.simulated import Fault
from richletsgo.exchange.simulated import FaultKind as F
from richletsgo.oms.journal import ProtectiveJournal
from richletsgo.oms.synchronizer import Synchronizer
from richletsgo.portfolio.models import ActionKind
from richletsgo.portfolio.trailing import recoveries
from richletsgo.risk.records import Direction


async def account(tmp_path, *, short=False, faults=()):
    db, clock, svc, router, ex, sync = setup(tmp_path, faults)
    assert await sync.synchronize()
    admission = await router.handle(
        signal(direction=Direction.SHORT if short else Direction.LONG), market(), RISK_INPUT
    )
    await sync.submit(admission.order)
    bar(clock, ex)
    await sync.tick()
    clock.advance(timedelta(seconds=1))
    await sync.tick()
    return db, clock, svc, router, ex, sync


async def target(db, svc, value):
    async with db.unit_of_work() as u:
        svc.trail_stop(u, StrategyId.S1, BTC, market().instrument, Price(D(value)))
        await u.commit()


async def attempts(db):
    async with db.unit_of_work() as u:
        return recoveries(u, include_archived=True)


async def restart(db, clock, svc, router, ex, sync, tmp_path):
    await sync.close()
    fresh = Synchronizer(
        db.unit_of_work,
        ex,
        svc,
        ProtectiveJournal(tmp_path),
        clock,
        router.gate.pauses,
        sync.sender.unique,
        POLICY,
        (BTC,),
    )
    await fresh.synchronize()
    return fresh


@pytest.mark.parametrize(
    "short,desired,aligned,worse",
    [(False, "94.129", "94.12", "93"), (True, "105.121", "105.13", "106")],
)
def test_target_is_aligned_monotonic_and_each_confirmed_stop_amended(
    tmp_path, short, desired, aligned, worse
):
    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path, short=short)
        await target(db, svc, desired)
        await target(db, svc, worse)
        assert (await book(db)).stop_price == Price(D(aligned))
        async with db.unit_of_work() as u:
            assert not any(a.kind is ActionKind.AMEND_STOP for a in u.portfolio.actions())
        await svc.tick()
        await svc.tick()
        async with db.unit_of_work() as u:
            assert len([a for a in u.portfolio.actions() if a.kind is ActionKind.AMEND_STOP]) == 1
        await sync.tick()
        await sync.tick()
        assert [o.trigger_price.value for o in await ex.get_open_orders(BTC)] == [D(aligned)]
        assert len(await attempts(db)) == 1 and (await attempts(db))[0].attempts == 1
        assert (await attempts(db))[0].in_flight is None
        await sync.close()

    asyncio.run(scenario())


def test_wrong_instrument_leaves_both_accounts_and_outbox_unchanged(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path)
        # A second open episode must not make the wrong instrument look valid.
        other = replace(signal(), symbol=Symbol.ETHUSDT)
        await router.handle(other, market(Symbol.ETHUSDT), RISK_INPUT)
        async with db.unit_of_work() as u:
            before = (u.portfolio.books(), u.portfolio.actions(), u.recovery.items())
            with pytest.raises(ValueError):
                svc.trail_stop(
                    u, StrategyId.S1, BTC, market(Symbol.ETHUSDT).instrument, Price(D(95))
                )
            with pytest.raises(ValueError):
                svc.trail_stop(u, StrategyId.S1, BTC, None, Price(D(95)))
            assert (u.portfolio.books(), u.portfolio.actions(), u.recovery.items()) == before
        await sync.close()

    asyncio.run(scenario())


def test_unsupported_retries_new_keys_then_pauses_without_replacement(tmp_path):
    async def scenario():
        faults = (Fault("amend", 1, F.AMEND_UNSUPPORTED), Fault("amend", 2, F.AMEND_UNSUPPORTED))
        db, clock, svc, router, ex, sync = await account(tmp_path, faults=faults)
        await target(db, svc, "95")
        await sync.tick()
        await sync.tick()
        assert (await attempts(db))[0].attempts == 1
        clock.advance(timedelta(seconds=5))
        await sync.tick()
        await sync.tick()
        assert (await attempts(db))[0].attempts == 2
        sync = await restart(db, clock, svc, router, ex, sync, tmp_path)
        clock.advance(timedelta(seconds=6))
        assert not await sync.synchronize()
        async with db.unit_of_work() as u:
            assert u.portfolio.paused(BTC)
            assert not any(k.startswith("replacement:") for k, v in u.recovery.items())
        requests = [r for r in sync.journal.read() if r.phase == "requested"]
        assert len(requests) == 2 and requests[0].action_key != requests[1].action_key
        assert [o.trigger_price.value for o in await ex.get_open_orders(BTC)] == [D(90)]
        await sync.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["TIMEOUT", "Accepted"])
def test_unapplied_response_waits_then_retries_with_new_key(tmp_path, monkeypatch, code):
    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path)
        original = ex.amend_stop
        sent = []

        async def delayed(link, trigger_price=None, qty=None):
            sent.append(trigger_price)
            if len(sent) == 1:
                return (
                    Accepted(None, link)
                    if code == "Accepted"
                    else ExchangeError(ErrorCode.TIMEOUT, "not applied")
                )
            return await original(link, trigger_price, qty)

        monkeypatch.setattr(ex, "amend_stop", delayed)
        await target(db, svc, "95")
        await sync.tick()
        for _ in range(3):
            await sync.tick()
        assert len(sent) == 1 and (await attempts(db))[0].in_flight
        sync = await restart(db, clock, svc, router, ex, sync, tmp_path)
        assert len(sent) == 1
        clock.advance(timedelta(seconds=5))
        await sync.tick()
        await sync.tick()
        await sync.tick()
        assert len(sent) == 2 and (await attempts(db))[0].in_flight is None
        assert (await attempts(db))[0].attempts == 2
        assert [o.trigger_price.value for o in await ex.get_open_orders(BTC)] == [D(95)]
        await sync.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("crash", ["requested", "applied", "response"])
def test_crash_boundaries_replay_attempt_without_duplicate(tmp_path, monkeypatch, crash):
    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path)
        original = ex.amend_stop

        async def interrupted(link, trigger_price=None, qty=None):
            if crash != "requested":
                await original(link, trigger_price, qty)
            raise RuntimeError("process exit")

        await target(db, svc, "95")
        if crash == "response":
            await sync.tick()
        else:
            monkeypatch.setattr(ex, "amend_stop", interrupted)
            with pytest.raises(RuntimeError):
                await sync.tick()
        monkeypatch.setattr(ex, "amend_stop", original)
        sync = await restart(db, clock, svc, router, ex, sync, tmp_path)
        state = (await attempts(db))[0]
        assert state.attempts == 1
        assert bool(state.in_flight) == (crash == "requested")
        assert len([r for r in sync.journal.read() if r.phase == "requested"]) == 1
        await sync.close()

    asyncio.run(scenario())


def test_stale_and_closing_actions_are_discarded_without_attempts(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path)
        await target(db, svc, "95")
        await svc.tick()
        await target(db, svc, "98")
        await svc.tick()
        await sync._actions(NOW)
        assert [s.target.value for s in await attempts(db) if s.attempts] == [D(98)]
        assert all(s.attempts == 0 for s in await attempts(db) if s.target.value != 98)
        await sync.tick()
        await target(db, svc, "99")
        await svc.tick()
        await router.handle(signal(kind="EXIT", bar=clock.now()), market(), RISK_INPUT)
        await target(db, svc, "100")
        assert (await book(db)).stop_price.value == 99
        await sync._actions(NOW)
        assert [s.target.value for s in await attempts(db) if s.attempts] == [D(98)]
        assert all(s.attempts == 0 for s in await attempts(db) if s.target.value != 98)
        await sync.close()

    asyncio.run(scenario())


def test_new_target_does_not_discard_old_inflight(tmp_path, monkeypatch):
    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path)

        async def delayed(link, trigger_price=None, qty=None):
            return Accepted(None, link)

        monkeypatch.setattr(ex, "amend_stop", delayed)
        await target(db, svc, "95")
        await sync.tick()
        await target(db, svc, "98")
        await sync.tick()
        assert len(await attempts(db)) == 1
        assert (await attempts(db))[0].target.value == 95
        clock.advance(timedelta(seconds=5))
        await sync.tick()
        await sync.tick()
        assert [s.target.value for s in await attempts(db)] == [D(95), D(98)]
        assert (await attempts(db))[1].in_flight
        await sync.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("short", [False, True])
def test_late_entry_fill_keeps_target_and_new_episode_resets_it(tmp_path, short):
    from richletsgo.risk.records import EpisodeState

    async def scenario():
        db, clock, svc, router, ex, sync = setup(
            tmp_path, [Fault("fill", 1, F.PARTIAL_FILL, value=D(".5"))]
        )
        assert await sync.synchronize()
        sig = signal(direction=Direction.SHORT if short else Direction.LONG)
        entry = (await router.handle(sig, market(), RISK_INPUT)).order
        entry = replace(entry, limit_price=Price(D(100)))
        async with db.unit_of_work() as u:
            u.orders.put(entry)
            await u.commit()
        await sync.submit(entry)
        remote = ex._orders["sim-order-1"]
        ex._fill(remote, Price(D(100)), clock.now(), maker=True)
        await sync.tick()
        desired = "105" if short else "95"
        clock.advance(timedelta(seconds=1))
        await target(db, svc, desired)
        await sync.tick()
        await sync.tick()
        old_episode = (await book(db)).stop_episode_id
        ex._fill(remote, Price(D(100)), clock.now(), maker=True)
        await sync.tick()
        await sync.tick()
        assert (await book(db)).stop_price.value == D(desired)
        assert (await book(db)).stop_episode_id == old_episode
        assert len(await ex.get_open_orders(BTC)) == 2
        assert all(o.trigger_price.value == D(desired) for o in await ex.get_open_orders(BTC))
        # Close both partial-fill stops, then open a distinct episode through the gate.
        for stop in tuple(ex._orders.values()):
            if stop.request.trigger_price and not stop.state.is_terminal:
                ex._fill(stop, Price(D(desired)), clock.now(), maker=False)
        await sync.tick()
        bar(clock, ex)  # settle the reduce-only close queued after the first stop fill
        for _ in range(6):
            clock.advance(timedelta(seconds=1))
            await sync.tick()
        assert await sync.synchronize()
        async with db.unit_of_work() as u:
            assert u.episodes.get(old_episode).state is EpisodeState.CLOSED
        nxt = await router.handle(replace(sig, bar_start=clock.now()), market(), RISK_INPUT)
        assert nxt.order is not None
        await sync.submit(nxt.order)
        bar(clock, ex)
        await sync.tick()
        assert (await book(db)).stop_price.value == (D(110) if short else D(90))
        assert (await book(db)).stop_episode_id != old_episode
        await sync.close()

    asyncio.run(scenario())


def test_late_open_aligns_and_lost_success_does_not_resend(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = await account(
            tmp_path, faults=[Fault("amend", 1, F.RESPONSE_LOST)]
        )
        async with db.unit_of_work() as u:
            stop = next(o for o in u.orders.all() if o.purpose is OrderPurpose.ATTACHED_SL)
            u.orders.put(replace(stop, state=OrderState.UNKNOWN))
            await u.commit()
        await target(db, svc, "95")
        await svc.tick()
        async with db.unit_of_work() as u:
            assert not any(a.kind is ActionKind.AMEND_STOP for a in u.portfolio.actions())
        await sync.tick()
        sync = await restart(db, clock, svc, router, ex, sync, tmp_path)
        clock.advance(timedelta(seconds=6))
        await sync.tick()
        assert (await attempts(db))[0].attempts == 1
        assert (await attempts(db))[0].in_flight is None
        assert (await book(db)).stop_price.value == 95
        await sync.close()

    asyncio.run(scenario())


def test_three_daily_targets_then_stop_executes_at_latest_trigger(tmp_path):
    from richletsgo.marketdata.models import Bar, Interval

    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path)
        start = clock.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        clock.set(start)
        for value in ("92", "94", "96"):
            await target(db, svc, value)
            await sync.tick()
            daily = Bar(BTC, Interval.DAY, start, D(100), D(105), D(99), D(100), D(1), D(100))
            clock.set(daily.end)
            ex.advance_bar(daily)
            await sync.tick()
            assert [o.trigger_price.value for o in await ex.get_open_orders(BTC)] == [D(value)]
            start = daily.end
        fall = Bar(BTC, Interval.DAY, start, D(100), D(101), D(95), D(97), D(1), D(100))
        clock.set(fall.end)
        ex.advance_bar(fall)
        await sync.tick()
        assert (await book(db)).qty == 0
        fills = await ex.get_executions(BTC, NOW)
        assert fills[-1].price.value == 96
        await sync.close()

    asyncio.run(scenario())


def test_db_failure_journal_restores_attempt_and_does_not_replay_trigger(tmp_path, monkeypatch):
    from richletsgo.storage.memory import InMemoryUnitOfWork

    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path)
        await target(db, svc, "95")
        await svc.tick()
        async with db.unit_of_work() as u:
            action = next(a for a in u.portfolio.actions() if a.kind is ActionKind.AMEND_STOP)
            stop = u.orders.get(action.order_key)
        original = InMemoryUnitOfWork.commit

        async def broken(self):
            raise OSError("DB unavailable")

        monkeypatch.setattr(InMemoryUnitOfWork, "commit", broken)
        await sync.sender.amend(
            stop, trigger=action.trigger, action_key=action.key, trail_attempt=0
        )
        monkeypatch.setattr(InMemoryUnitOfWork, "commit", original)
        assert await attempts(db) == ()
        sync = await restart(db, clock, svc, router, ex, sync, tmp_path)
        assert (await attempts(db))[0].attempts == 1
        assert (await attempts(db))[0].in_flight is None
        assert len([r for r in sync.journal.read() if r.phase == "requested"]) == 1
        await sync.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failed_query", [False, True])
def test_delayed_application_and_failed_query_keep_single_attempt(
    tmp_path, monkeypatch, failed_query
):
    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path)
        original = ex.amend_stop
        requests = []

        async def delayed(link, trigger_price=None, qty=None):
            requests.append((link, trigger_price, qty))
            return Accepted(None, link)

        monkeypatch.setattr(ex, "amend_stop", delayed)
        await target(db, svc, "95")
        await sync.tick()
        assert (await attempts(db))[0].in_flight
        if failed_query:

            async def unavailable(symbol, since):
                return ExchangeError(ErrorCode.TIMEOUT, "query unavailable")

            monkeypatch.setattr(ex, "get_order_history", unavailable)
            clock.advance(timedelta(seconds=6))
            await sync.tick()
            await sync.tick()
            assert (await attempts(db))[0].in_flight
            assert len(requests) == 1
        # Venue applies the accepted request later; its event alone can settle it,
        # including while REST is down. It is not a second sender request.
        await original(*requests[0])
        await sync.tick()
        assert (await attempts(db))[0].in_flight is None
        assert (await attempts(db))[0].attempts == 1 and len(requests) == 1
        await sync.close()

    asyncio.run(scenario())


def test_already_improved_stop_discards_queued_action(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path)
        await target(db, svc, "95")
        await svc.tick()
        async with db.unit_of_work() as u:
            stop = next(o for o in u.orders.all() if o.purpose is OrderPurpose.ATTACHED_SL)
            u.orders.put(replace(stop, trigger_price=Price(D(98))))
            await u.commit()
        await sync._actions(NOW)
        assert all(s.attempts == 0 and s.discards == 1 for s in await attempts(db))
        assert not sync.journal.read()
        await sync.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("reboot", [False, True])
def test_discard_during_other_strategy_close_requeues_same_target(tmp_path, reboot):
    from richletsgo.portfolio.models import CloseIntent, CloseState

    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path)
        await target(db, svc, "95")
        await svc.tick()
        closing = CloseIntent("s2-close", "s2-episode", StrategyId.S2, BTC, clock.now())
        async with db.unit_of_work() as u:
            original_key = next(
                a.key for a in u.portfolio.actions() if a.kind is ActionKind.AMEND_STOP
            )
            u.portfolio.put_intent(closing)
            await u.commit()
        await sync._actions(NOW)
        state = (await attempts(db))[0]
        assert state.attempts == 0 and state.discards == 1
        assert state.last_attempt_at is None and state.in_flight is None
        assert not sync.journal.read()
        async with db.unit_of_work() as u:
            assert not u.portfolio.actions()
        if reboot:
            sync = await restart(db, clock, svc, router, ex, sync, tmp_path)
            assert (await attempts(db))[0] == state
        async with db.unit_of_work() as u:
            u.portfolio.put_intent(replace(closing, state=CloseState.DONE))
            await u.commit()
        await svc.tick()
        await svc.tick()
        async with db.unit_of_work() as u:
            actions = [a for a in u.portfolio.actions() if a.kind is ActionKind.AMEND_STOP]
            assert len(actions) == 1 and actions[0].key != original_key
        await sync.tick()
        await sync.tick()
        assert [o.trigger_price.value for o in await ex.get_open_orders(BTC)] == [D(95)]
        state = (await attempts(db))[0]
        assert state.attempts == 1 and state.discards == 1 and state.in_flight is None
        await sync.close()

    asyncio.run(scenario())
