"""M2-03 deterministic integration through actual risk/portfolio/OMS/exchange."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from test_risk import limits

from richletsgo.backtest.config import ResearchProfile, load_research_config
from richletsgo.backtest.runner import BacktestRun
from richletsgo.domain import Symbol
from richletsgo.marketdata.models import (
    Bar,
    FundingRate,
    InstrumentSnapshot,
    Interval,
    RiskLimitTier,
)
from richletsgo.marketdata.store import MarketDataStore
from richletsgo.oms.synchronizer import SyncPolicy
from richletsgo.portfolio.models import ClosePolicy
from richletsgo.strategy.models import S1AParams, S2AParams

START = datetime(2026, 1, 3, tzinfo=UTC)
HOUR = timedelta(hours=1)
SYMBOLS = tuple(Symbol)


def run_config(hours=12, **changes):
    profile = ResearchProfile(
        limits(
            risk_per_trade=D(".01"),
            max_leverage=D(3),
            max_drawdown_ratio=D(".9"),
            daily_loss_limit_usdt=D(200),
            max_symbol_notional_usdt=D(20000),
            max_account_notional_usdt=D(30000),
        ),
        D(1000),
        D(".001"),
        D(".0002"),
        D(0),
    )
    return replace(
        BacktestRun(
            START,
            START + hours * HOUR,
            S1AParams(1, 2, 1),
            S2AParams(3, D(1), 2, D(1), 24),
            profile,
            ClosePolicy(3, 3, timedelta(seconds=5), "LastPrice"),
            SyncPolicy(
                timedelta(days=7),
                timedelta(minutes=5),
                timedelta(seconds=20),
                timedelta(seconds=5),
                3,
            ),
            SYMBOLS,
            START,
            "test-commit",
        ),
        **changes,
    )


def populate(store, hours=12, *, omit=None, funding_omit=None, missing_tier=False, prices=None):
    for symbol in SYMBOLS:
        store.put_instrument(
            InstrumentSnapshot(
                symbol, D(".001"), D(".001"), D(".01"), D(100), timedelta(hours=8), START
            )
        )
        if not missing_tier:
            store.put_risk_tiers(
                [RiskLimitTier(symbol, 1, D(100000), D(".005"), D(".01"), D(100), START)]
            )
        bars = []
        for i in range(-48, hours):
            at = START + i * HOUR
            value = D(95 if i < -24 else 100 if i < 0 else 101)
            if prices is not None and i >= 0:
                value = D(prices(i, symbol))
            if (symbol, at) == omit:
                continue
            bars.append(
                Bar(symbol, Interval.HOUR, at, value, value + 1, value - 1, value, D(1), value)
            )
        store.put_bars(bars)
        days = []
        for offset in range(-2, hours // 24):
            day = START + timedelta(days=offset)
            day_bars = [b for b in bars if day <= b.start < day + timedelta(days=1)]
            if len(day_bars) == 24:
                days.append(
                    Bar(
                        symbol,
                        Interval.DAY,
                        day,
                        day_bars[0].open,
                        max(b.high for b in day_bars),
                        min(b.low for b in day_bars),
                        day_bars[-1].close,
                        D(24),
                        sum(b.turnover for b in day_bars),
                    )
                )
        store.put_bars(days)
        rates = [
            FundingRate(symbol, START + i * HOUR, D(".001"))
            for i in range(-8, hours, 8)
            if (symbol, START + i * HOUR) != funding_omit
        ]
        store.put_funding(rates)


def execute(tmp_path, config=None, **populate_args):
    config = config or run_config()
    with MarketDataStore(tmp_path / "market") as store:
        populate(store, int((config.end - START) // HOUR), **populate_args)
        return asyncio.run(
            config.execute(
                store, results_dir=tmp_path / "results", journal_dir=tmp_path / "journal"
            )
        )


def test_small_run_prices_fees_funding_and_sqlite(tmp_path):
    result = execute(tmp_path)
    assert result.status == "COMPLETED"
    assert len(result.trades) == 2
    # Signal close=100, prior exit channel=94. Risk budget 10 / (6+.001*194) => 1.614.
    for trade in result.trades:
        assert D(trade["entry_qty"]) == D("1.614")
        assert D(trade["entry_price"]) == 101  # next bar open, not signal close
        assert D(trade["exit_price"]) == 101
        assert trade["opened_at"] == START.isoformat()
        assert trade["closed_at"] == (START + 11 * HOUR).isoformat()
        assert D(trade["fees"]) == D("1.614") * 101 * D(".001") * 2
        assert D(trade["funding"]) == D("1.614") * 101 * D(".001")
        assert D(trade["net_pnl"]) == -(D(trade["fees"]) + D(trade["funding"]))
        assert D(trade["r_multiple"]) == D(trade["net_pnl"]) / 10
        assert trade["reasons"] == ["end_of_range"]
    assert result.equity[-1]["wallet_balance"] == 1000 + sum(D(t["net_pnl"]) for t in result.trades)
    path = tmp_path / "results" / (result.result_hash + ".sqlite3")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 2
        assert (
            json.loads(
                db.execute("SELECT value_json FROM metadata WHERE key='result_hash'").fetchone()[0]
            )
            == result.result_hash
        )


@pytest.mark.parametrize("missing", ["bar", "one_contract", "funding", "tier"])
def test_invalid_data_prevents_all_execution(tmp_path, missing):
    with MarketDataStore(tmp_path / "market") as store:
        populate(
            store,
            24,
            omit=(SYMBOLS[0], START + 2 * HOUR) if missing == "bar" else None,
            funding_omit=(SYMBOLS[1], START + 8 * HOUR) if missing == "funding" else None,
            missing_tier=missing == "tier",
        )
        if missing == "one_contract":
            with sqlite3.connect(store.path) as db:
                db.execute("DELETE FROM bars WHERE symbol=?", (SYMBOLS[1].value,))
        result = asyncio.run(
            run_config(24).execute(
                store, results_dir=tmp_path / "results", journal_dir=tmp_path / "journal"
            )
        )
        assert result.status == "INVALID_DATA" and result.issues
        assert result.trades == result.orders == result.executions == result.trace == ()
        assert not (tmp_path / "journal").exists()


def test_deterministic_repeat_and_future_data_ignored(tmp_path):
    with MarketDataStore(tmp_path / "market") as store:
        populate(store)
        run = run_config()
        first = asyncio.run(
            run.execute(store, results_dir=tmp_path / "a", journal_dir=tmp_path / "journal")
        )
        for symbol in SYMBOLS:
            store.put_bars(
                [Bar(symbol, Interval.HOUR, run.end, D(999), D(1000), D(998), D(999), D(1), D(999))]
            )
            store.put_funding([FundingRate(symbol, run.end, D(1))])
        second = asyncio.run(
            run.execute(store, results_dir=tmp_path / "b", journal_dir=tmp_path / "journal")
        )
        assert first.result_hash == second.result_hash


def test_d88_loader_paths_and_no_risk_defaults(tmp_path):
    path = tmp_path / "config.toml"
    source = Path(__file__).parents[1] / "config.example.toml"
    path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    config = load_research_config(path)
    assert config.results_dir == tmp_path / "backtest_results/backtest"
    assert config.profile.taker_fee == D(".00055") and config.profile.maker_fee == D(".0002")
    assert config.selection["max_grid_points"] == 24  # D91
    path.write_text(
        source.read_text(encoding="utf-8").replace('risk_per_trade = "0.01"', ""), encoding="utf-8"
    )
    with pytest.raises(KeyError):
        load_research_config(path)


@pytest.mark.parametrize("held", [False, True])
def test_tier_cap_is_atomic_across_strategies(held):
    from test_risk import BTC, S1, S2, figures, make_gate, market, request

    from richletsgo.domain import Environment, Qty
    from richletsgo.risk.records import DecisionKind, RejectionCode
    from richletsgo.storage.memory import InMemoryDatabase

    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        gate = make_gate(db, lim=limits(risk_per_trade=D(".006")))
        m = replace(market(), tier_notional_limit=D(100))
        risk = {S1: figures(), S2: figures()}
        if held:
            async with db.unit_of_work() as u:
                u.positions.set(S1, BTC, Qty(D(".6")))
                await u.commit()
        else:
            first = await gate.admit(request(signal="s1", strategy=S1), m, risk)
            assert first.decision.kind is DecisionKind.ADMITTED
        second = await gate.admit(request(signal="s2", strategy=S2), m, risk)
        assert second.decision.kind is DecisionKind.REJECTED
        assert second.decision.rejections[0].code is RejectionCode.TIER_NOTIONAL
        async with db.unit_of_work() as u:
            assert len(u.orders.all()) == (0 if held else 1)

    asyncio.run(scenario())


def test_midnight_order_funding_and_wallet_baseline(tmp_path):
    def prices(i, symbol):
        return 101 if i == 0 else 101 if i == 22 else 103 if i == 23 else 104 if i >= 24 else 105

    result = execute(tmp_path, run_config(27), prices=prices)
    at = START + 24 * HOUR
    stages = [(r["stage"], r["symbol"]) for r in result.trace if r["at"] == at]
    assert stages[:4] == [("fills", None), ("funding", None), ("start_day", None), ("risk", None)]
    assert stages[4:] == [
        ("signal:S1", SYMBOLS[0]),
        ("signal:S1", SYMBOLS[1]),
        ("signal:S2", SYMBOLS[0]),
        ("signal:S2", SYMBOLS[1]),
    ]
    snapshot = next(r for r in result.equity if r["at"] == at)
    # Two existing S1 positions, one entry fee and three settlements each.
    assert snapshot["baseline"] == 1000 - 2 * D("1.614") * (101 + 105 + 105 + 103) * D(".001")
    s2 = [t for t in result.trades if t["strategy"] == "S2"]
    assert len(s2) == 2
    assert all(t["opened_at"] == at.isoformat() and D(t["funding"]) == 0 for t in s2)
    assert snapshot["baseline"] == snapshot["wallet_balance"]
    assert snapshot["equity"] > snapshot["baseline"]  # unrealized gains aren't the baseline


def test_midnight_carried_loss_repauses_after_daily_expiry(tmp_path):
    conf = run_config(50)
    conf = replace(
        conf,
        profile=replace(
            conf.profile, limits=replace(conf.profile.limits, daily_loss_limit_usdt=D(2))
        ),
    )
    # Stop remains 94; close 98 is below actual entry 101 but not stopped.
    result = execute(tmp_path, conf, prices=lambda i, s: 101 if i < 20 else 98)
    boundary = START + 24 * HOUR
    record = next(r for r in result.pauses if r["at"] == boundary)
    daily = [p for p in record["active"] if p["reason"] == "L5"]
    assert len(daily) == 1 and daily[0]["since"] == boundary.isoformat()
    assert all(
        not boundary <= e.event.exec_time < boundary + timedelta(days=1)
        for e in result.executions
        if next(o for o in result.orders if o.key == e.order_key).purpose.value == "E"
    )


def test_stop_loss_and_slippage_are_venue_fills(tmp_path):
    conf = run_config()
    conf = replace(conf, profile=replace(conf.profile, slippage=D(".001")))
    result = execute(tmp_path, conf, prices=lambda i, s: 101 if i < 3 else 93)
    assert len(result.trades) == 2
    for t in result.trades:
        assert t["reasons"] == ["stop_loss"]
        assert D(t["entry_price"]) == D("101.101")
        assert D(t["exit_price"]) == D("92.907")  # gap through stop=94, then adverse slippage
        assert t["closed_at"] == (START + 4 * HOUR - timedelta(microseconds=1)).isoformat()


def test_run_has_no_wall_clock_or_network_dependency(tmp_path, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    result = execute(tmp_path)
    assert result.status == "COMPLETED"
    assert all(START <= r["at"] <= START + 12 * HOUR for r in result.equity)


def test_execution_timestamp_is_provenance_not_result_identity(tmp_path):
    first = execute(tmp_path / "a")
    second = execute(tmp_path / "b", replace(run_config(), executed_at=START + timedelta(days=1)))
    assert first.metadata["executed_at"] != second.metadata["executed_at"]
    assert first.metadata["config_hash"] == second.metadata["config_hash"]
    assert first.result_hash == second.result_hash


@pytest.mark.parametrize("state_name", ["UNKNOWN", "PARTIALLY_FILLED"])
def test_tier_cap_counts_uncertain_and_unbooked_entry(state_name):
    from test_risk import S1, S2, entry_order, figures, make_gate, market, request

    from richletsgo.domain import Environment, OrderState, Qty
    from richletsgo.risk.records import RejectionCode
    from richletsgo.storage.memory import InMemoryDatabase

    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        gate = make_gate(db, lim=limits(risk_per_trade=D(".006")))
        old = entry_order(qty=".5", state=OrderState(state_name))
        if state_name == "PARTIALLY_FILLED":
            old = replace(old, exchange_cum_exec_qty=D(".2"), booked_qty=Qty(D(0)))
        async with db.unit_of_work() as u:
            u.orders.put(old)
            await u.commit()
        result = await gate.admit(
            request(signal="s2", strategy=S2),
            replace(market(), tier_notional_limit=D(100)),
            {S1: figures(), S2: figures()},
        )
        assert result.decision.rejections[0].code is RejectionCode.TIER_NOTIONAL

    asyncio.run(scenario())


def test_concurrent_tier_reservations_are_not_independent(tmp_path):
    from test_risk import S1, S2, figures, make_gate, market, request

    from richletsgo.domain import Environment
    from richletsgo.risk.records import DecisionKind, RejectionCode
    from richletsgo.storage.memory import InMemoryDatabase

    async def scenario():
        db = InMemoryDatabase("acct", Environment.DEMO)
        gate = make_gate(db, lim=limits(risk_per_trade=D(".006")))
        m = replace(market(), tier_notional_limit=D(100))
        results = await asyncio.gather(
            *(
                gate.admit(request(signal=s.value, strategy=s), m, {S1: figures(), S2: figures()})
                for s in (S1, S2)
            )
        )
        assert [r.decision.kind for r in results].count(DecisionKind.ADMITTED) == 1
        rejected = next(r for r in results if r.decision.kind is DecisionKind.REJECTED)
        assert rejected.decision.rejections[0].code is RejectionCode.TIER_NOTIONAL

    asyncio.run(scenario())


@pytest.mark.parametrize("hour", [0, 16])
def test_missing_first_or_last_funding_is_invalid(tmp_path, hour):
    result = execute(tmp_path, run_config(24), funding_omit=(SYMBOLS[0], START + hour * HOUR))
    assert result.status == "INVALID_DATA"
    assert any(i.kind == "funding_gap" for i in result.issues)


def test_final_one_hour_is_entry_disabled(tmp_path):
    result = execute(tmp_path, run_config(1))
    assert result.status == "COMPLETED" and not result.trades and not result.orders
    assert result.equity[-1]["equity"] == 1000


def test_parameter_and_environment_validation(tmp_path):
    with pytest.raises(ValueError):
        run_config(symbols=(SYMBOLS[0],))
    with pytest.raises(ValueError):
        run_config(start=START + timedelta(minutes=1))
    with pytest.raises(ValueError):
        run_config(end=START)
    path = tmp_path / "live.toml"
    source = (Path(__file__).parents[1] / "config.example.toml").read_text(encoding="utf-8")
    path.write_text(
        source.replace('environment = "backtest"', 'environment = "live"'), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="backtest"):
        load_research_config(path)


@pytest.mark.parametrize("candidate", ["A", "B"])
def test_stop_closed_episode_can_enter_again_on_later_days(tmp_path, candidate):
    from richletsgo.strategy.models import S1BParams

    conf = run_config(96)
    if candidate == "B":
        conf = replace(conf, s1=S1BParams(1, 1))

    def prices(i, symbol):
        if i < 6:
            return 102
        if i < 8:
            return 93
        return 105 + (i // 24)

    result = execute(tmp_path, conf, prices=prices)
    s1 = [t for t in result.trades if t["strategy"] == "S1"]
    assert len(s1) >= 4
    assert any(t["opened_at"] >= (START + timedelta(days=1)).isoformat() for t in s1)


@pytest.mark.parametrize("s1_kind", ["A", "B"])
@pytest.mark.parametrize("s2_kind", ["A", "B"])
def test_candidate_combinations_close_and_reconcile(tmp_path, s1_kind, s2_kind):
    from richletsgo.strategy.models import S1BParams, S2BParams

    conf = run_config(96)
    if s1_kind == "B":
        conf = replace(conf, s1=S1BParams(1, 1))
    if s2_kind == "B":
        conf = replace(conf, s2=S2BParams(2, D(30), D(70), D(50), 2, D(1), 24))
    result = execute(tmp_path, conf, prices=lambda i, s: 103 + (i // 24) + (i % 8 == 0))
    assert result.status == "COMPLETED"
    assert not result.remaining_positions
    assert result.trades
    net = sum((D(t["net_pnl"]) for t in result.trades), D(0))
    assert abs(result.equity[-1]["equity"] - conf.profile.start_equity - net) < D("1e-18")
