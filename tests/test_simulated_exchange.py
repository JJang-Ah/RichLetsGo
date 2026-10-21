"""Execution rules and deterministic exchange failures, with no network access."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from richletsgo.domain import (
    ExecutionEvent,
    InstrumentSpec,
    ManualClock,
    OrderEvent,
    OrderPurpose,
    OrderState,
    Price,
    Qty,
    Symbol,
)
from richletsgo.exchange.contracts import (
    Accepted,
    ErrorCode,
    ExchangeError,
    ExchangeGateway,
    OrderRequest,
)
from richletsgo.exchange.simulated import Fault, FaultPlan, SimulatedExchange, StopReduction
from richletsgo.exchange.simulated import FaultKind as F
from richletsgo.marketdata.models import Bar, FundingRate, Interval
from richletsgo.portfolio.models import StopParameters

T = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)
S = Symbol.BTCUSDT


def run(coro):
    return asyncio.run(coro)


def setup(*faults, reduction=StopReduction.NEWEST, preferred=None, slip=D(0)):
    clock = ManualClock(T)
    ex = SimulatedExchange(
        instruments=[InstrumentSpec(S, D(".1"), D(".1"), D(".1"), D(5))],
        clock=clock,
        wallet_balance=D(1000),
        maker_fee=D(".001"),
        taker_fee=D(".002"),
        slippage=lambda price, qty: Price(price.value + (slip if qty.value > 0 else -slip)),
        faults=FaultPlan(faults),
        stop_reduction=reduction,
        preferred_parent=preferred,
    )
    return ex, clock


def entry(link="entry", qty="1", limit=None, stop=None, expires=None):
    q = D(qty)
    return OrderRequest(
        link,
        S,
        Qty(q),
        OrderPurpose.ENTRY,
        Price(D(100)),
        Price(D(limit)) if limit else None,
        StopParameters(Price(D(stop or ("90" if q > 0 else "110"))), "LastPrice"),
        expires_at=expires,
    )


def bar(n=0, *, op="100", low="95", high="105", close="100"):
    return Bar(S, Interval.HOUR, T + n * H, D(op), D(high), D(low), D(close), D(1), D(100))


def step(ex, clock, n=0, funding=(), **kwargs):
    b = bar(n, **kwargs)
    clock.set(b.end)
    ex.advance_bar(b, funding=funding)


def fills(ex):
    return run(ex.get_executions(S, T))


def orders(ex):
    return run(ex.get_order_history(S, T))


def position(ex):
    return run(ex.get_positions())[0].qty.value


def close(ex, qty="-1", link="close"):
    return run(
        ex.place_order(OrderRequest(link, S, Qty(D(qty)), OrderPurpose.CLOSE, Price(D(100))))
    )


def test_protocol_next_open_slippage_and_wallet():
    ex, clock = setup(slip=D(1))
    gateway: ExchangeGateway = ex
    assert run(gateway.place_order(entry())) == Accepted("entry", "sim-order-1")
    # A signal submitted at T cannot fill in the candle ending at T.
    ex.advance_bar(bar(-1))
    assert fills(ex) == ()
    step(ex, clock)
    (execution,) = fills(ex)
    assert execution.price.value == 101 and execution.exec_time == T
    assert execution.fee == D(".202") and not execution.is_maker
    account = run(ex.get_account())
    assert account.wallet_balance == D("999.798")
    assert account.unrealized_pnl == -1


@pytest.mark.parametrize("qty,low,high", [("1", "100", "105"), ("-1", "95", "100")])
def test_limit_touch_is_not_cross_and_default_expires(qty, low, high):
    ex, clock = setup()
    run(ex.place_order(entry(qty=qty, limit="100")))
    step(ex, clock, low=low, high=high)
    assert fills(ex) == ()
    assert orders(ex)[0].state is OrderState.EXPIRED


@pytest.mark.parametrize("qty", ["1", "-1"])
def test_limit_cross_maker_fee(qty):
    ex, clock = setup(slip=D(2))
    run(ex.place_order(entry(qty=qty, limit="100")))
    step(ex, clock)
    (e,) = fills(ex)
    assert e.price.value == 100 and e.fee == D(".1") and e.is_maker


@pytest.mark.parametrize(
    "qty,op,low,high,expected",
    [
        ("1", "85", "80", "88", "84"),
        ("-1", "115", "112", "120", "116"),
        ("1", "100", "90", "105", "89"),
        ("-1", "100", "95", "110", "111"),
    ],
)
def test_stop_gap_and_touch(qty, op, low, high, expected):
    ex, clock = setup(slip=D(1))
    run(ex.place_order(entry(qty=qty)))
    step(ex, clock)
    step(ex, clock, 1, op=op, low=low, high=high, close=op)
    assert fills(ex)[-1].price.value == D(expected)
    assert position(ex) == 0


def test_entry_bar_stop_and_parent_link():
    ex, clock = setup()
    run(ex.place_order(entry()))
    step(ex, clock, low="89")
    assert [e.qty.value for e in fills(ex)] == [1, -1]
    stop = orders(ex)[1]
    assert stop.parent_order_link_id == "entry"
    assert stop.order_link_id is None and stop.state is OrderState.FILLED


def test_stop_wins_over_profitable_market_exit():
    ex, clock = setup()
    run(ex.place_order(entry()))
    step(ex, clock)
    close(ex)
    step(ex, clock, 1, op="110", high="115", low="85")
    assert fills(ex)[-1].price.value == 90
    assert orders(ex)[-1].state is OrderState.CANCELLED
    assert position(ex) == 0


def test_two_partial_fills_create_two_stops():
    ex, clock = setup(Fault("fill", 1, F.PARTIAL_FILL, "entry", D(".5")))
    run(ex.place_order(entry(qty="2", limit="100", expires=T + 2 * H)))
    step(ex, clock)
    step(ex, clock, 1)
    stops = [o for o in orders(ex) if o.parent_order_link_id]
    assert len(stops) == 2
    assert [o.order_qty.value for o in stops] == [-1, -1]
    assert all(o.parent_order_link_id == "entry" for o in stops)
    assert position(ex) == 2


@pytest.mark.parametrize(
    "order_request", [entry(qty=".01"), entry(stop="101"), replace(entry(), stop=None)]
)
def test_invalid_entries_rejected(order_request):
    ex, _ = setup()
    result = run(ex.place_order(order_request))
    assert result.code is ErrorCode.REJECTED
    assert orders(ex) == ()


def test_qty_rounding_and_reduce_only_clips():
    ex, clock = setup()
    run(ex.place_order(entry(qty="1.29")))
    step(ex, clock)
    assert position(ex) == D("1.2")
    close(ex, "-5")
    step(ex, clock, 1)
    assert fills(ex)[-1].qty.value == D("-1.2") and position(ex) == 0
    close(ex, "-1", "again")
    step(ex, clock, 2)
    assert position(ex) == 0 and len(fills(ex)) == 2


def test_funding_after_prior_bar_stop_before_next_open_at_midnight():
    ex, clock = setup()
    clock.set(T - H)
    run(ex.place_order(entry()))
    clock.set(T)
    rate = FundingRate(S, T, D(".01"))
    ex.advance_bar(bar(-1), funding=[rate])
    assert ex.funding_payments[0].amount == -1
    close(ex)
    step(ex, clock)
    assert position(ex) == 0
    assert run(ex.get_account()).wallet_balance == D("998.6")


def test_funding_zero_after_stop_and_nonfixed_timestamp():
    ex, clock = setup()
    run(ex.place_order(entry()))
    step(ex, clock, low="89", funding=[FundingRate(S, T + H, D(".01"))])
    assert ex.funding_payments[0].qty.value == 0
    assert ex.funding_payments[0].amount == 0


def test_funding_inside_bar_rejected_before_mutation():
    ex, clock = setup()
    run(ex.place_order(entry()))
    clock.set(T + H)
    with pytest.raises(ValueError):
        ex.advance_bar(bar(), funding=[FundingRate(S, T + H / 2, D(".01"))])
    assert fills(ex) == ()


@pytest.mark.parametrize(
    "reduction,preferred,cancelled",
    [
        (StopReduction.NEWEST, None, "b"),
        (StopReduction.OLDEST, None, "a"),
        (StopReduction.PREFERRED_PARENT, "a", "a"),
    ],
)
def test_stop_reduction_policy(reduction, preferred, cancelled):
    ex, clock = setup(reduction=reduction, preferred=preferred)
    run(ex.place_order(entry("a")))
    run(ex.place_order(entry("b")))
    step(ex, clock)
    close(ex)
    step(ex, clock, 1)
    stops = [o for o in orders(ex) if o.parent_order_link_id]
    assert (
        next(o for o in stops if o.state is OrderState.CANCELLED).parent_order_link_id == cancelled
    )
    assert position(ex) == 1


def test_response_lost_visibility_delay_and_no_duplicate_retry():
    ex, _ = setup(Fault("place", 1, F.RESPONSE_LOST), Fault("place", 1, F.HIDE_ORDER, value=D(2)))
    assert run(ex.place_order(entry())).code is ErrorCode.TIMEOUT
    assert run(ex.get_open_orders(S)) == ()
    assert run(ex.get_order_history(S, T)) == ()
    assert len(run(ex.get_open_orders(S))) == 1
    assert run(ex.place_order(entry())).code is ErrorCode.REJECTED


@pytest.mark.parametrize(
    "operation", ["open_orders", "history", "executions", "positions", "account", "instrument"]
)
def test_query_faults_are_one_shot(operation):
    ex, _ = setup(Fault(operation, 1, F.QUERY_ERROR))
    funcs = {
        "open_orders": lambda: ex.get_open_orders(S),
        "history": lambda: ex.get_order_history(S, T),
        "executions": lambda: ex.get_executions(S, T),
        "positions": ex.get_positions,
        "account": ex.get_account,
        "instrument": lambda: ex.get_instrument(S),
    }
    assert run(funcs[operation]()).code is ErrorCode.QUERY_FAILED
    assert not isinstance(run(funcs[operation]()), ExchangeError)


def test_order_before_execution_and_duplicate_do_not_duplicate_accounting():
    ex, clock = setup(Fault("fill", 1, F.ORDER_FIRST), Fault("fill", 1, F.DUPLICATE))
    run(ex.place_order(entry()))
    ex.drain_events()
    step(ex, clock)
    events = ex.drain_events()
    relevant = [
        e
        for e in events
        if isinstance(e, ExecutionEvent)
        or isinstance(e, OrderEvent)
        and e.state is OrderState.FILLED
    ]
    assert isinstance(relevant[0], OrderEvent)
    assert relevant[0] == relevant[1] and relevant[2] == relevant[3]
    assert len(fills(ex)) == 1 and position(ex) == 1


def test_attached_execution_precedes_parent_link_event():
    ex, clock = setup(Fault("place", 1, F.HOLD_ATTACHED))
    run(ex.place_order(entry()))
    step(ex, clock, low="89")
    events = ex.drain_events()
    execution_index = next(
        i for i, e in enumerate(events) if isinstance(e, ExecutionEvent) and e.order_link_id is None
    )
    parent_index = next(
        i for i, e in enumerate(events) if isinstance(e, OrderEvent) and e.parent_order_link_id
    )
    assert execution_index < parent_index


def test_disconnect_stream_ends_rest_keeps_fills_and_reconnect():
    async def scenario():
        ex, clock = setup(Fault("fill", 1, F.DISCONNECT))
        await ex.place_order(entry())
        stream = ex.private_events()
        await anext(stream)
        step(ex, clock)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert not ex.connected
        assert len(await ex.get_executions(S, T)) == 1
        ex.reconnect()
        assert ex.connected and ex.drain_events() == ()

    run(scenario())


@pytest.mark.parametrize(
    "fault,expected", [(F.PARTIAL_FILL, OrderState.CANCELLED), (F.REJECT, OrderState.REJECTED)]
)
def test_ioc_partial_or_rejected(fault, expected):
    ex, clock = setup(Fault("fill", 1, fault, "close", D(".5")))
    run(ex.place_order(entry(qty="2")))
    step(ex, clock)
    close(ex, "-2")
    step(ex, clock, 1)
    assert orders(ex)[-1].state is expected
    assert position(ex) == (1 if fault is F.PARTIAL_FILL else 2)


@pytest.mark.parametrize(
    "fault,code",
    [
        (F.CANCEL_NOT_FOUND, ErrorCode.NOT_FOUND),
        (F.CANCEL_TOO_LATE, ErrorCode.TOO_LATE),
        (F.RESPONSE_LOST, ErrorCode.TIMEOUT),
    ],
)
def test_cancel_errors(fault, code):
    ex, _ = setup(Fault("cancel", 1, fault))
    run(ex.place_order(entry()))
    assert run(ex.cancel_order("entry")).code is code
    assert orders(ex)[0].state is (
        OrderState.CANCELLED if fault is F.RESPONSE_LOST else OrderState.OPEN
    )


def test_cancel_fill_race_emits_filled_twice():
    ex, clock = setup(Fault("cancel", 1, F.CANCEL_RACE))
    step(ex, clock)
    run(ex.place_order(entry()))
    assert run(ex.cancel_order("entry")).code is ErrorCode.TOO_LATE
    states = [
        e for e in ex.drain_events() if isinstance(e, OrderEvent) and e.state is OrderState.FILLED
    ]
    assert len(states) == 2 and states[0] == states[1] and len(fills(ex)) == 1


def test_amend_attached_stop_by_exchange_id_response_loss():
    ex, clock = setup(Fault("amend", 1, F.RESPONSE_LOST))
    run(ex.place_order(entry()))
    step(ex, clock)
    stop = orders(ex)[1]
    assert run(ex.amend_stop(stop.exchange_order_id, Price(D(92)))).code is ErrorCode.TIMEOUT
    step(ex, clock, 1, low="91")
    assert fills(ex)[-1].price.value == 92


def test_determinism():
    def scenario():
        ex, clock = setup(Fault("fill", 1, F.DUPLICATE))
        run(ex.place_order(entry()))
        step(ex, clock, low="89")
        return fills(ex), orders(ex), ex.drain_events(), run(ex.get_account())

    assert scenario() == scenario()


def test_repeated_or_future_bar_rejected():
    ex, clock = setup()
    with pytest.raises(ValueError):
        ex.advance_bar(bar())
    step(ex, clock)
    with pytest.raises(ValueError):
        ex.advance_bar(bar())


def test_partial_stop_reduction_preserves_cumulative_and_parent():
    ex, clock = setup()
    run(ex.place_order(entry(qty="2")))
    step(ex, clock)
    close(ex, "-.5")
    step(ex, clock, 1)
    stop = next(o for o in orders(ex) if o.parent_order_link_id)
    assert stop.order_qty.value == D("-1.5") and stop.exchange_cum_exec_qty == 0
    assert stop.parent_order_link_id == "entry"
    step(ex, clock, 2, low="89")
    assert position(ex) == 0 and fills(ex)[-1].qty.value == D("-1.5")


def test_entry_ioc_partial_has_only_filled_quantity_protected():
    ex, clock = setup(Fault("fill", 1, F.PARTIAL_FILL, value=D(".5")))
    run(ex.place_order(entry(qty="2")))
    step(ex, clock)
    assert orders(ex)[0].state is OrderState.CANCELLED
    assert orders(ex)[1].order_qty.value == -1 and position(ex) == 1


def test_opposite_entry_rejected_at_fill_even_if_submitted_flat():
    ex, clock = setup()
    run(ex.place_order(entry("long")))
    run(ex.place_order(entry("short", qty="-1")))
    step(ex, clock)
    assert position(ex) == 1
    assert next(o for o in orders(ex) if o.order_link_id == "short").state is OrderState.REJECTED


def test_expired_order_never_fills_and_intrabar_expiry_requires_finer_data():
    ex, clock = setup()
    run(ex.place_order(entry(limit="100", expires=T + H / 2)))
    clock.set(T + H)
    with pytest.raises(ValueError, match="smaller bars"):
        ex.advance_bar(bar())
    assert fills(ex) == ()
    step(ex, clock, 1)
    assert orders(ex)[0].state is OrderState.EXPIRED and fills(ex) == ()


def test_amend_and_cancel_missing_or_terminal():
    ex, clock = setup()
    assert run(ex.cancel_order("missing")).code is ErrorCode.NOT_FOUND
    assert run(ex.amend_stop("missing", Price(D(90)))).code is ErrorCode.NOT_FOUND
    run(ex.place_order(entry()))
    assert run(ex.amend_stop("entry", Price(D(90)))).code is ErrorCode.REJECTED
    step(ex, clock)
    assert run(ex.cancel_order("entry")).code is ErrorCode.TOO_LATE
    stop = orders(ex)[1]
    assert isinstance(run(ex.cancel_order(stop.exchange_order_id)), Accepted)
    assert run(ex.amend_stop(stop.exchange_order_id, Price(D(90)))).code is ErrorCode.TOO_LATE


def test_fault_occurrence_and_order_filter():
    ex, _ = setup(Fault("place", 2, F.REJECT))
    assert isinstance(run(ex.place_order(entry("a"))), Accepted)
    assert run(ex.place_order(entry("b"))).code is ErrorCode.REJECTED
    assert isinstance(run(ex.place_order(entry("c"))), Accepted)


def test_short_funding_credit_and_realized_profit():
    ex, clock = setup()
    run(ex.place_order(entry(qty="-1")))
    step(ex, clock, funding=[FundingRate(S, T + H, D(".01"))])
    assert ex.funding_payments[0].amount == 1
    close(ex, "1")
    step(ex, clock, 1, op="95", low="94", high="100", close="95")
    assert run(ex.get_account()).wallet_balance == D("1005.610")


@pytest.mark.parametrize(
    "fault",
    [
        lambda: Fault("fill", 1, F.RESPONSE_LOST),
        lambda: Fault("fill", 0, F.DUPLICATE),
        lambda: Fault("fill", 1, F.PARTIAL_FILL, value=D("1.1")),
        lambda: Fault("place", 1, F.HIDE_ORDER, value=D(".5")),
    ],
)
def test_invalid_fault_plan_rejected(fault):
    with pytest.raises(ValueError):
        fault()


def test_release_held_delivers_untriggered_stop_after_parent_execution():
    ex, clock = setup(Fault("place", 1, F.HOLD_ATTACHED))
    run(ex.place_order(entry()))
    step(ex, clock)
    before = ex.drain_events()
    assert any(isinstance(e, ExecutionEvent) for e in before)
    assert not any(isinstance(e, OrderEvent) and e.parent_order_link_id for e in before)
    stop = next(o for o in orders(ex) if o.parent_order_link_id)
    assert stop.state is OrderState.OPEN
    account_before = run(ex.get_account())
    executions_before = fills(ex)
    for n in (1, 2):
        step(ex, clock, n)
    ex.release_held()
    assert ex.drain_events() == (stop,)
    assert position(ex) == 1 and fills(ex) == executions_before
    assert run(ex.get_account()).wallet_balance == account_before.wallet_balance
    ex.release_held()
    assert ex.drain_events() == ()
    step(ex, clock, 3, low="89")
    events = ex.drain_events()
    assert not any(
        isinstance(e, OrderEvent) and e.parent_order_link_id and e.state is OrderState.OPEN
        for e in events
    )
    assert position(ex) == 0


def test_release_held_after_automatic_release_does_not_duplicate():
    ex, clock = setup(Fault("place", 1, F.HOLD_ATTACHED))
    run(ex.place_order(entry()))
    step(ex, clock, low="89")
    events = ex.drain_events()
    assert (
        sum(
            isinstance(e, OrderEvent)
            and e.parent_order_link_id is not None
            and e.state is OrderState.OPEN
            for e in events
        )
        == 1
    )
    ex.release_held()
    assert ex.drain_events() == ()


def test_release_held_while_disconnected_drops_events_but_preserves_rest():
    ex, clock = setup(Fault("place", 1, F.HOLD_ATTACHED))
    run(ex.place_order(entry()))
    step(ex, clock)
    ex.drain_events()
    rest_before = orders(ex)
    ex.disconnect()
    ex.release_held()
    ex.reconnect()
    ex.release_held()
    assert ex.drain_events() == ()
    assert orders(ex) == rest_before and position(ex) == 1


def test_release_held_multiple_stops_preserves_creation_order():
    ex, clock = setup(Fault("place", 1, F.HOLD_ATTACHED), Fault("place", 2, F.HOLD_ATTACHED))
    run(ex.place_order(entry("a")))
    run(ex.place_order(entry("b")))
    step(ex, clock)
    ex.drain_events()
    ex.release_held()
    assert [e.parent_order_link_id for e in ex.drain_events()] == ["a", "b"]
