"""Boundary tests for M1-01; all numeric fixtures are test-only."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import UUID

import pytest

from richletsgo.domain import (
    Clock,
    Environment,
    ExecutionEvent,
    FixedClock,
    InstrumentSpec,
    ManualClock,
    OrderEvent,
    OrderPurpose,
    OrderState,
    OrderType,
    PositionSnapshot,
    Price,
    Qty,
    Side,
    StopKind,
    StrategyId,
    Symbol,
    TimeInForce,
    make_order_link_id,
    new_operation_id,
    parse_order_link_id,
    side_and_abs,
    signed,
    trading_day_start,
)

D = Decimal
NOW = datetime(2026, 10, 20, 23, 59, 59, tzinfo=UTC)


@pytest.fixture
def instrument():
    return InstrumentSpec(Symbol.BTCUSDT, D("0.005"), D("0.01"), D("0.25"), D("5"))


@pytest.fixture
def execution():
    return ExecutionEvent(
        "fill-1",
        None,
        "exchange-1",
        Symbol.BTCUSDT,
        Qty(D("-0.4")),
        Price(D("100")),
        D("-0.01"),
        NOW,
        True,
    )


@pytest.fixture
def order():
    return OrderEvent(
        None,
        "exchange-1",
        "d-S1-E-parent",
        OrderState.OPEN,
        D(0),
        NOW,
        {},
        Symbol.BTCUSDT,
        Qty(D(-1)),
        OrderType.MARKET,
        TimeInForce.IOC,
        None,
        Price(D(90)),
        True,
        StopKind.PARTIAL_STOP_LOSS,
    )


@pytest.mark.parametrize("factory", [Qty, Price])
@pytest.mark.parametrize("bad", [1.0, 1, True, "1", None])
def test_numeric_wrappers_reject_implicit_conversion(factory, bad):
    with pytest.raises(TypeError):
        factory(bad)


@pytest.mark.parametrize("factory", [Qty, Price])
@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_numeric_wrappers_reject_nonfinite(factory, bad):
    with pytest.raises(ValueError):
        factory(D(bad))


@pytest.mark.parametrize("bad", ["0", "-0", "-1"])
def test_price_rejects_nonpositive(bad):
    with pytest.raises(ValueError):
        Price(D(bad))


def test_qty_allows_flat_and_both_directions():
    assert [Qty(D(x)).value for x in ("-1", "0", "1")] == [D(-1), D(0), D(1)]


@pytest.mark.parametrize("bad", [" btcusdt\n", "btcusdt", "BTCUSDT ", "BTCUSD", ""])
def test_symbol_is_exact(bad):
    with pytest.raises(ValueError):
        Symbol(bad)


@pytest.mark.parametrize("value,side", [("1.23456789", Side.BUY), ("-1.23456789", Side.SELL)])
def test_adapter_conversion_round_trip_is_exact_under_low_precision(value, side):
    qty = Qty(D(value))
    with localcontext() as ctx:
        ctx.prec = 3
        actual_side, absolute = side_and_abs(qty)
        assert actual_side is side
        assert absolute == D("1.23456789")
        assert signed(actual_side, absolute) == qty


@pytest.mark.parametrize("zero", ["0", "-0"])
def test_zero_has_no_order_side(zero):
    with pytest.raises(ValueError):
        side_and_abs(Qty(D(zero)))
    with pytest.raises(ValueError):
        signed(Side.BUY, D(zero))


def test_signed_rejects_negative_absolute_or_non_enum():
    with pytest.raises(ValueError):
        signed(Side.SELL, D(-1))
    with pytest.raises(TypeError):
        signed("SELL", D(1))


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", "0"),
        ("0.0000001", "0"),
        ("0.0099", "0"),
        ("0.01", "0.01"),
        ("0.0149", "0.01"),
        ("0.015", "0.015"),
    ],
)
def test_qty_rounding_does_not_inflate_risk(instrument, value, expected):
    assert instrument.round_qty_down(D(value)) == D(expected)


@pytest.mark.parametrize(
    "value,down,up",
    [
        ("100.24", "100", "100.25"),
        ("100.25", "100.25", "100.25"),
        ("0.01", "0", "0.25"),
    ],
)
def test_non_power_of_ten_tick(instrument, value, down, up):
    assert instrument.round_price_down(D(value)) == D(down)
    assert instrument.round_price_up(D(value)) == D(up)


def test_rounding_preserves_boundary_under_small_decimal_context(instrument):
    with localcontext() as ctx:
        ctx.prec = 2
        assert instrument.round_price_down(D("100.249999999999999999999999999999")) == D(100)
        assert instrument.round_price_up(D("100.250000000000000000000000000001")) == D("100.5")
        assert instrument.round_qty_down(D("123456.789")) == D("123456.785")


@pytest.mark.parametrize("field", ["qty_step", "min_qty", "tick_size", "max_leverage"])
@pytest.mark.parametrize("bad", [D(0), D(-1), D("NaN"), D("Infinity"), 0.1])
def test_instrument_invalid_fields(instrument, field, bad):
    with pytest.raises((TypeError, ValueError)):
        replace(instrument, **{field: bad})


def test_invalid_leverage_and_symbol(instrument):
    with pytest.raises(ValueError):
        replace(instrument, max_leverage=D("0.5"))
    with pytest.raises(TypeError):
        replace(instrument, symbol="BTCUSDT")


@pytest.mark.parametrize("method", ["round_qty_down", "round_price_down", "round_price_up"])
@pytest.mark.parametrize("bad", [D(-1), D("NaN"), D("Infinity"), 1.0])
def test_rounding_rejects_invalid_input(instrument, method, bad):
    with pytest.raises((TypeError, ValueError)):
        getattr(instrument, method)(bad)


def test_state_ranks_and_terminal_set():
    active = [
        OrderState.CREATED,
        OrderState.SUBMITTED,
        OrderState.UNKNOWN,
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
    ]
    terminal = {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
        OrderState.NOT_FOUND,
        OrderState.ABORTED,
    }
    assert all(left.rank < right.rank for left, right in zip(active, active[1:], strict=False))
    assert {state for state in OrderState if state.is_terminal} == terminal
    assert len({state.rank for state in terminal}) == 1
    assert all(state.rank > active[-1].rank for state in terminal)


@pytest.mark.parametrize("env", list(Environment))
@pytest.mark.parametrize("strategy", list(StrategyId))
@pytest.mark.parametrize("purpose", [OrderPurpose.ENTRY, OrderPurpose.CLOSE, OrderPurpose.PROTECT])
def test_order_id_round_trip_at_max_length(env, strategy, purpose):
    unique = "a" * 29
    value = make_order_link_id(env, strategy, purpose, unique_factory=lambda: unique)
    assert len(value) == 36
    parsed = parse_order_link_id(value)
    assert (parsed.environment, parsed.strategy, parsed.purpose, parsed.unique_value) == (
        env,
        strategy,
        purpose,
        unique,
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "d-S1-E-",
        "d-S1-E-" + "a" * 30,
        "D-S1-E-a",
        "d-S3-E-a",
        "d-S1-Q-a",
        "d-S1-E-a\n",
        " d-S1-E-a",
        "d-S1-E-한글",
        "exchange-assigned-123",
    ],
)
def test_foreign_or_malformed_id_is_not_ours(value):
    assert parse_order_link_id(value) is None


@pytest.mark.parametrize("suffix", ["", "a" * 30, "hello world", "한글", "x\n"])
def test_id_generation_rejects_bad_suffix(suffix):
    with pytest.raises(ValueError):
        make_order_link_id(
            Environment.DEMO, StrategyId.S1, OrderPurpose.ENTRY, unique_factory=lambda: suffix
        )


def test_id_generator_injection_and_exchange_owned_stop():
    values = iter(["first_id", "second-id"])
    first = make_order_link_id(
        Environment.DEMO, StrategyId.S1, OrderPurpose.ENTRY, unique_factory=lambda: next(values)
    )
    second = make_order_link_id(
        Environment.DEMO, StrategyId.S1, OrderPurpose.ENTRY, unique_factory=lambda: next(values)
    )
    assert first != second
    assert parse_order_link_id(second).unique_value == "second-id"
    with pytest.raises(ValueError):
        make_order_link_id(
            Environment.DEMO,
            StrategyId.S1,
            OrderPurpose.ATTACHED_SL,
            unique_factory=lambda: "unused",
        )
    fixed = UUID("12345678-1234-5678-1234-567812345678")
    assert new_operation_id(uuid_factory=lambda: fixed) == str(fixed)
    with pytest.raises(TypeError):
        new_operation_id(uuid_factory=lambda: "not a UUID")


@pytest.mark.parametrize(
    "bad", [datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=9)))]
)
def test_non_utc_rejected_at_all_event_and_clock_boundaries(execution, order, bad):
    for make in (
        lambda: replace(execution, exec_time=bad),
        lambda: replace(order, event_time=bad),
        lambda: PositionSnapshot(Symbol.BTCUSDT, Qty(D(0)), None, bad),
        lambda: FixedClock(bad),
        lambda: ManualClock(bad),
        lambda: trading_day_start(bad),
    ):
        with pytest.raises(ValueError):
            make()
    clock = ManualClock(NOW)
    with pytest.raises(ValueError):
        clock.set(bad)
    assert clock.now() == NOW


def test_trading_day_boundary_and_clock_protocol():
    fixed: Clock = FixedClock(NOW)
    manual = ManualClock(NOW)
    assert fixed.now() == manual.now()
    assert trading_day_start(manual.now()) == datetime(2026, 10, 20, tzinfo=UTC)
    manual.advance(timedelta(seconds=1))
    assert trading_day_start(manual.now()) == datetime(2026, 10, 21, tzinfo=UTC)
    assert fixed.now() == NOW


def test_event_payloads_cannot_be_mutated_from_source_or_reader(order):
    source = {"orderStatus": "New", "nested": {"ids": ["a", "b"]}}
    event = replace(order, raw=source)
    source["nested"]["ids"].append("c")
    assert event.raw["nested"]["ids"] == ("a", "b")
    with pytest.raises(TypeError):
        event.raw["nested"]["ids"] = ()
    with pytest.raises(FrozenInstanceError):
        event.state = OrderState.FILLED


@pytest.mark.parametrize(
    "field,bad",
    [
        ("qty", Qty(D(0))),
        ("qty", D(1)),
        ("price", D(100)),
        ("fee", D("NaN")),
        ("fee", 1.0),
        ("symbol", "BTCUSDT"),
        ("exec_id", ""),
        ("exchange_order_id", ""),
        ("order_link_id", ""),
        ("is_maker", 1),
    ],
)
def test_execution_validates_runtime_boundaries(execution, field, bad):
    with pytest.raises((TypeError, ValueError)):
        replace(execution, **{field: bad})


def test_pending_attribution_and_rebate_are_representable(execution, order):
    assert execution.order_link_id is None
    assert execution.fee < 0
    assert order.parent_order_link_id == "d-S1-E-parent"
    assert replace(order, parent_order_link_id=None).parent_order_link_id is None
    with pytest.raises(FrozenInstanceError):
        execution.qty = Qty(D(1))


@pytest.mark.parametrize(
    "field,bad",
    [
        ("exchange_cum_exec_qty", D(-1)),
        ("exchange_cum_exec_qty", D("Infinity")),
        ("exchange_cum_exec_qty", 1.0),
        ("state", "OPEN"),
        ("raw", {"bad": object()}),
        ("raw", {1: "bad key"}),
        ("raw", []),
    ],
)
def test_order_event_validation(order, field, bad):
    with pytest.raises((TypeError, ValueError)):
        replace(order, **{field: bad})


def test_position_prices_and_immutability():
    flat = PositionSnapshot(Symbol.BTCUSDT, Qty(D(0)), None, NOW)
    with pytest.raises(ValueError):
        replace(flat, qty=Qty(D(1)))
    long = replace(flat, qty=Qty(D(1)), avg_price=Price(D(100)))
    assert long.avg_price.value == D(100)
    assert replace(long, qty=Qty(D(-1))).qty.value == D(-1)
    with pytest.raises(TypeError):
        replace(flat, avg_price=0)
    with pytest.raises(FrozenInstanceError):
        flat.qty = Qty(D(1))
