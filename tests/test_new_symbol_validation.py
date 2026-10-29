"""D97 offline contracts. All series here are generated, never actual prices."""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal as D

import pytest
import test_backtest as fixtures
from test_walkforward import NOW, fixture_config

from richletsgo.backtest.codec import digest, encode
from richletsgo.backtest.paired import paired
from richletsgo.backtest.registration import check_collection
from richletsgo.backtest.statistics import aggregate
from richletsgo.backtest.walkforward import run_campaign
from richletsgo.backtest.wf_config import load_walk_config
from richletsgo.domain import Symbol
from richletsgo.marketdata.ingest import main as ingest_main
from richletsgo.marketdata.store import MarketDataStore
from richletsgo.strategy.models import S1BParams, S2PParams

NEW = (Symbol.SOLUSDT, Symbol.ZECUSDT, Symbol.XRPUSDT, Symbol.NEARUSDT)


def pair_fixture(tmp_path, monkeypatch, *, omit=None):
    monkeypatch.setattr(fixtures, "SYMBOLS", NEW)
    path, _ = fixture_config(tmp_path, omit=omit)
    source = path.read_text().split("[backtest.grid]")[0]
    source = source.replace(
        "[backtest.research_risk]",
        "[backtest]\nsymbols = "
        + json.dumps([s.value for s in NEW])
        + "\n[backtest.research_risk]",
    )
    source += '\n[backtest.preregistration]\nid="synthetic-D97"\nsha256="' + "a" * 64 + '"\n'
    grid = "\n[backtest.grid]\nS1B = [{entry_channel=1, exit_channel=1}]\n"
    primary = tmp_path / "primary.toml"
    primary.write_text(
        (source + grid).replace(
            "min_training_trades = { S1 = 0, S2 = 0 }", "min_training_trades = { S1 = 0 }"
        )
    )
    secondary = tmp_path / "secondary.toml"
    secondary.write_text(
        source
        + grid
        + 'S2P = [{rsi_period=1, lower="20", atr_period=1, stop_atr="2", target_r="2", max_hold_bars=4}]\n'
    )
    return primary, secondary


@pytest.mark.parametrize(
    "symbols,joint", [(NEW, False), (tuple(Symbol), False), (tuple(Symbol), True)]
)
def test_offline_universe_s1_only_has_no_s2_events(tmp_path, monkeypatch, symbols, joint):
    monkeypatch.setattr(fixtures, "SYMBOLS", symbols)
    run = fixtures.run_config(
        72,
        symbols=symbols,
        s1=S1BParams(1, 1),
        s2=S2PParams(1, D(20), 1, D(2), D(2), 4) if joint else None,
    )
    with MarketDataStore(tmp_path / "market") as store:
        fixtures.populate(store, 72, prices=lambda i, s: 101 + i // 12)
        result = asyncio.run(
            run.execute(store, results_dir=tmp_path / "r", journal_dir=tmp_path / "j")
        )
    assert result.status == "COMPLETED"
    assert result.orders
    if not joint:
        assert all(o.strategy.value != "S2" for o in result.orders)
        assert not any("signal:S2" in str(row) for row in result.trace)
    assert (
        run.profile.limits.strategy_allocations
        == fixtures.run_config().profile.limits.strategy_allocations
    )
    assert {o.symbol.value for o in result.orders} <= {s.value for s in symbols}


def test_same_snapshot_pair_resume_and_disabled_breakdown(tmp_path, monkeypatch):
    primary, secondary = pair_fixture(tmp_path, monkeypatch)
    root = paired(primary, secondary, "test", NOW, prepare_only=True)
    audit = json.loads((root / "pair.json").read_text())
    assert audit["sha256"] == digest(audit["body"])
    campaigns = load_walk_config(primary).research.results_dir / "campaigns"
    manifests = [
        json.loads((campaigns / k / "manifest.json").read_text())
        for k in audit["body"]["campaigns"]
    ]
    assert manifests[0]["identity"]["data_hash"] == manifests[1]["identity"]["data_hash"]
    for path in (primary, secondary):
        output = run_campaign(path, "test", NOW)
        first = (output / "report-data.json").read_bytes()
        run_campaign(path, "test", NOW)
        assert first == (output / "report-data.json").read_bytes()
        body = json.loads(first)
        if path == primary:
            assert body["aggregates"]["S1B"]["by_strategy"]["S2"] == {}
        assert "descriptive_periods" in next(iter(body["aggregates"].values()))


@pytest.mark.parametrize("table", ["risk_limit_tiers", "funding", "bars"])
def test_prepare_failure_is_final_for_both_before_calculation(tmp_path, monkeypatch, table):
    primary, secondary = pair_fixture(tmp_path, monkeypatch)
    import sqlite3

    db = load_walk_config(primary).research.market_data_dir / "market_data.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute(f"DELETE FROM {table} WHERE symbol=?", (NEW[0].value,))
    root = paired(primary, secondary, "test", NOW, prepare_only=False)
    raw = (root / "pair.json").read_bytes()
    record = json.loads(raw)
    assert record["body"]["primary"] == record["body"]["secondary"] == "INVALID_INPUT"
    assert record["sha256"] == digest(record["body"])
    assert record["body"]["symbol"] == NEW[0].value
    assert record["body"]["ingest_runs"]
    paired(primary, secondary, "test", NOW + timedelta(days=1), prepare_only=False)
    assert (root / "pair.json").read_bytes() == raw
    assert not list(load_walk_config(primary).research.results_dir.glob("campaigns/*/artifacts/*"))

    record["body"]["item"] = "tampered"
    (root / "pair.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="digest"):
        paired(primary, secondary, "test", NOW, prepare_only=True)


def test_confirmation_gap_invalidates_both_even_without_candidate(tmp_path, monkeypatch):
    primary, secondary = pair_fixture(
        tmp_path, monkeypatch, omit=(NEW[0], fixtures.START + timedelta(days=40))
    )
    for path in (primary, secondary):
        output = run_campaign(path, "test", NOW)
        body = json.loads((output / "report-data.json").read_text())
        assert next(iter(body["windows"].values()))[0]["status"] == "INVALID_DATA"


def test_new_symbol_guard_runs_before_io(tmp_path, monkeypatch):
    primary, _ = pair_fixture(tmp_path, monkeypatch)
    text = primary.read_text()
    text = text.replace(
        '[backtest.preregistration]\nid="synthetic-D97"\nsha256="' + "a" * 64 + '"', ""
    )
    primary.write_text(text)

    def never(*args, **kwargs):
        raise AssertionError("must fail before transport/store access")

    monkeypatch.setattr("richletsgo.backtest.walkforward.campaign_lock", never)
    with pytest.raises(ValueError, match="preregistration"):
        run_campaign(primary, "test", NOW)
    with pytest.raises(ValueError, match="preregistration"):
        ingest_main(
            [
                "--config",
                str(tmp_path / "absent.toml"),
                "--symbols",
                "SOLUSDT",
                "--intervals",
                "60",
            ],
            transport_factory=never,
        )


def test_registration_digest_universe_and_collection_bounds(tmp_path):
    body = {
        "id": "test",
        "symbols": [s.value for s in NEW],
        "collection": {"start": fixtures.START.isoformat(), "end_exclusive": NOW.isoformat()},
    }
    path = tmp_path / "registration.json"
    path.write_text(encode({"body": body, "sha256": digest(body)}))
    check_collection(path, NEW, fixtures.START, NOW)
    with pytest.raises(ValueError, match="outside"):
        check_collection(path, NEW, fixtures.START - timedelta(days=1), NOW)
    body["symbols"] = []
    path.write_text(encode({"body": body, "sha256": "a" * 64}))
    with pytest.raises(ValueError, match="digest"):
        check_collection(path, NEW, fixtures.START, NOW)


@pytest.mark.parametrize("net,expected", [("-1", "FAIL"), ("0", "FAIL"), ("1", "PASS")])
def test_secondary_requires_strict_positive_s2(net, expected):
    trade = {
        "closed_at": NOW.isoformat(),
        "symbol": "SOLUSDT",
        "strategy": "S2",
        "episode_id": "test",
        "net_pnl": net,
        "r_multiple": "0",
    }
    rows = [
        {
            "start": fixtures.START.isoformat(),
            "end": NOW.isoformat(),
            "status": "COMPLETED",
            "trades": [trade],
            "equity": [{"at": NOW.isoformat(), "equity": "110"}],
        }
    ]
    assert (
        aggregate(
            rows,
            D(100),
            D(".25"),
            {"min_net_pnl_usdt": D(0), "max_drawdown_ratio": D(".15")},
            strategy_minimum={"S2": D(0)},
        )["verdict"]
        == expected
    )


def test_retry_record_and_frozen_fingerprint(tmp_path):
    from test_marketdata import T0, FakeBybit

    from richletsgo.backtest.campaign import fingerprint

    now = T0 + timedelta(days=4)
    fake = FakeBybit(now)

    class Once:
        count = 0

        def get(self, url, timeout):
            self.count += 1
            if self.count == 1:
                return 429, b"{}"
            return fake.get(url, timeout)

    config = tmp_path / "config.toml"
    config.write_text(
        '[bybit_public]\nbase_url="https://api.example.test"\n[paths.backtest]\nmarket_data_dir="market"\n'
    )
    assert (
        ingest_main(
            [
                "--config",
                str(config),
                "--symbols",
                "BTCUSDT",
                "--intervals",
                "D",
                "60",
                "--since",
                T0.isoformat(),
                "--until",
                (now - timedelta(days=1)).isoformat(),
                "--preregistration",
                str(tmp_path / "legacy-no-registration-needed"),
                "--pause",
                "0",
                "--backoff",
                "0",
            ],
            now=lambda: now,
            transport_factory=Once,
            sleep=lambda _: None,
        )
        == 0
    )
    pinned = {Symbol.BTCUSDT: (now, now)}
    first = fingerprint(tmp_path / "market", T0, now, pinned)
    assert fingerprint(tmp_path / "market", T0, now, pinned) == first
    import sqlite3

    with sqlite3.connect(tmp_path / "market" / "market_data.sqlite3") as db:
        summary = json.loads(db.execute("SELECT summary_json FROM ingest_runs").fetchone()[0])
    assert [a["attempt"] for a in summary["request_attempts"][:2]] == [1, 2]
    assert all(a["at"] for a in summary["request_attempts"])
    assert summary["request"]["end"] != summary["request"]["now"]


def test_invariants_check_outside_configured_books():
    from richletsgo.domain import StrategyId
    from richletsgo.portfolio.models import StrategyBook
    from richletsgo.portfolio.service import invariants

    book = StrategyBook(StrategyId.S1, Symbol.SOLUSDT)
    assert any(
        "outside configured" in e
        for e in invariants([book], {}, symbols=frozenset({Symbol.BTCUSDT}))
    )
