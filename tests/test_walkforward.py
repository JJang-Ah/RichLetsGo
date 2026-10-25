"""M2-04: pinned inputs, independent selection, publication and resume contracts."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from test_backtest import HOUR, START, populate, run_config

from richletsgo.backtest.campaign import campaign_lock, prepare, verify_inputs
from richletsgo.backtest.codec import digest
from richletsgo.backtest.statistics import aggregate, choose
from richletsgo.backtest.walkforward import (
    ComputeLimit,
    cached,
    estimate,
    make_job,
    run_campaign,
)
from richletsgo.backtest.wf_config import load_walk_config, months
from richletsgo.domain import Symbol
from richletsgo.marketdata.models import to_ms
from richletsgo.marketdata.store import DB_FILE, MarketDataStore

NOW = datetime(2026, 4, 1, tzinfo=UTC)
EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"
TINY_GRID = """[backtest.grid]
S1A = [{ fast = 1, slow = 2, exit_channel = 1 }]
S1B = [{ entry_channel = 1, exit_channel = 1 }]
S2A = [{ period = 3, k = "1", atr_period = 2, atr_multiple = "1", max_hold_bars = 24 }]
S2B = [{ period = 1, low = "30", high = "70", neutral = "50", atr_period = 2, atr_multiple = "1", max_hold_bars = 24 }]
"""


def fixture_config(tmp_path, *, minimum=0, funding_omit=None, omit=None, conflicts=False):
    text = EXAMPLE.read_text(encoding="utf-8").split("[backtest.grid]")[0] + TINY_GRID
    text = text.replace("2023-10-01T00:00:00Z", "2026-01-03T00:00:00Z").replace(
        "2026-10-01T00:00:00Z", "2026-03-03T00:00:00Z"
    )
    text = (
        text.replace("train_months = 12", "train_months = 1")
        .replace("test_months = 3", "test_months = 1")
        .replace("workers = 4", "workers = 1")
    )
    text = text.replace("{ S1 = 6, S2 = 50 }", f"{{ S1 = {minimum}, S2 = {minimum} }}")
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    config = load_walk_config(path)
    hours = int((config.end - START) / HOUR)
    with MarketDataStore(config.research.market_data_dir) as store:
        populate(store, hours, funding_omit=funding_omit, omit=omit)
        summary = {
            "series": {
                "BTCUSDT:60": {
                    "conflicts": ["test conflict"] if conflicts else [],
                    "requested_ranges": [[START.isoformat(), (START + HOUR).isoformat()]],
                }
            }
        }
        store.record_run(
            started=NOW,
            finished=NOW,
            summary=summary,
            ok=not conflicts,
            package_version="fixture",
            code_commit="test",
        )
    return path, config


def test_calendar_grid_warmup_and_required_settings(tmp_path):
    config = load_walk_config(EXAMPLE)
    assert len(config.windows) == 8
    assert config.windows[0].train_end == datetime(2024, 10, 1, tzinfo=UTC)
    assert config.windows[-1].end == config.end
    assert config.warmup_start == config.start - timedelta(days=41)
    assert {len(pairs) for pairs in config.grids.values()} == {4}
    assert months(datetime(2024, 1, 31, tzinfo=UTC), 1).day == 29
    assert months(datetime(2025, 1, 31, tzinfo=UTC), 1).day == 28
    for old, new in [
        ("max_grid_points = 24", "max_grid_points = 3"),
        ("max_grid_points = 24", "max_grid_points = 25"),
        ("workers = 4", "workers = 0"),
        ("workers = 4", ""),
        ('k = "2"', "k = 2.0"),
    ]:
        path = tmp_path / "bad.toml"
        path.write_text(EXAMPLE.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        with pytest.raises((ValueError, KeyError, TypeError)):
            load_walk_config(path)


def test_summary_keeps_simulation_identical_without_full_sqlite(tmp_path):
    config = run_config(24)
    with MarketDataStore(tmp_path / "source") as store:
        populate(store, 24)
        full = asyncio.run(
            config.execute(store, results_dir=tmp_path / "full", journal_dir=tmp_path / "journal")
        )
        summary = asyncio.run(
            config.execute(
                store,
                results_dir=tmp_path / "summary",
                journal_dir=tmp_path / "journal",
                storage_policy="SUMMARY",
                input_key="pinned",
            )
        )
    assert full.result_hash == summary.result_hash
    assert not list((tmp_path / "summary").glob("*.sqlite3"))
    saved = json.loads(next((tmp_path / "summary").glob("*.json")).read_text())
    assert saved["input_key"] == "pinned"
    assert saved["status"] == "COMPLETED" and not saved["remaining_positions"]
    assert saved["trades_hash"] == digest(full.trades)


def test_pinning_readonly_and_changed_inputs_rejected(tmp_path):
    path, config = fixture_config(tmp_path)
    root, manifest = prepare(config, "test", NOW)
    with MarketDataStore(config.research.market_data_dir) as store:
        old = store.latest_instrument(Symbol.BTCUSDT)
        store.put_instrument(replace(old, captured_at=NOW, min_qty=D(5)))
    from richletsgo.backtest.campaign import captures

    with MarketDataStore(
        config.research.market_data_dir,
        readonly=True,
        captured_at=captures(manifest["identity"]["captured_at"]),
    ) as store:
        assert store.latest_instrument(Symbol.BTCUSDT) == old
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.put_instrument(replace(old, captured_at=NOW + HOUR))
    # Unpinned snapshots do not alter an already pinned input; new ingest provenance does.
    verify_inputs(root, manifest, config.research.market_data_dir)
    with MarketDataStore(config.research.market_data_dir) as store:
        store.record_run(
            started=NOW,
            finished=NOW,
            summary={},
            ok=True,
            package_version="test",
            code_commit="test",
        )
    with pytest.raises(ValueError, match="input changed"):
        verify_inputs(root, manifest, config.research.market_data_dir)
    with campaign_lock(config.research.results_dir), pytest.raises(ValueError, match="active"):
        with campaign_lock(config.research.results_dir):
            pass


@pytest.mark.parametrize("missing", ["bar", "funding"])
def test_insufficient_warmup_refused_before_workers(tmp_path, missing):
    _, config = fixture_config(
        tmp_path,
        omit=(Symbol.BTCUSDT, START - HOUR) if missing == "bar" else None,
        funding_omit=(Symbol.BTCUSDT, START - 8 * HOUR) if missing == "funding" else None,
    )
    if missing == "funding":
        # Also remove the rate at the start so no look-behind value can be used.
        with sqlite3.connect(config.research.market_data_dir / DB_FILE) as db:
            db.execute(
                "DELETE FROM funding WHERE symbol='BTCUSDT' AND funding_ms=?", (to_ms(START),)
            )
    with pytest.raises(ValueError, match="warm-up|look-behind"):
        prepare(config, "test", NOW)


def sample(net="10", dd="0", s1=6, s2=50, status="COMPLETED"):
    return {
        "status": status,
        "remaining_positions": [],
        "net_pnl": net,
        "max_drawdown": dd,
        "trade_counts": {"S1": s1, "S2": s2},
    }


def test_selection_floor_minimum_ties_and_no_candidate():
    minimum = {"S1": 6, "S2": 50}
    a, b, c = sample(), sample(s2=51), sample(s1=5, net="1000")
    assert choose([((D(10),), a), ((D(2),), b), ((D(1),), c)], D(100), minimum)[1] is b
    assert choose([((D(10),), a), ((D(2),), sample())], D(100), minimum)[0] == (D(2),)
    assert choose([((D(1),), c)], D(100), minimum) is None
    assert choose([((D(1),), sample(status="INCOMPLETE"))], D(100), minimum) is None
    assert choose([((D(1),), sample(dd="200")), ((D(2),), a)], D(100), minimum)[1] is a


def test_additive_curve_drawdown_and_missing_criteria():
    rows = [
        {
            "start": START.isoformat(),
            "end": (START + 2 * HOUR).isoformat(),
            "status": "COMPLETED",
            "trades": [],
            "equity": [
                {"at": START.isoformat(), "equity": "100"},
                {"at": (START + HOUR).isoformat(), "equity": "120"},
                {"at": (START + 2 * HOUR).isoformat(), "equity": "110"},
            ],
        },
        {
            "start": (START + 2 * HOUR).isoformat(),
            "end": (START + 4 * HOUR).isoformat(),
            "status": "COMPLETED",
            "trades": [],
            "equity": [
                {"at": (START + 2 * HOUR).isoformat(), "equity": "100"},
                {"at": (START + 3 * HOUR).isoformat(), "equity": "80"},
                {"at": (START + 4 * HOUR).isoformat(), "equity": "105"},
            ],
        },
    ]
    result = aggregate(rows, D(100), D(".25"), None)
    assert result["net_pnl"] == "15"
    assert result["max_drawdown"] == "30" and D(result["max_drawdown_ratio"]) == D(".25")
    assert result["verdict"] == "UNSET"
    rows[1] = {
        "start": (START + 2 * HOUR).isoformat(),
        "end": (START + 4 * HOUR).isoformat(),
        "status": "NO_CANDIDATE",
    }
    assert aggregate(rows, D(100), D(".25"), None)["verdict"] == "INDETERMINATE"
    assert aggregate(rows, D(100), D(".5"), None)["net_pnl"] == "10"


def test_resume_parallel_report_and_cache_integrity(tmp_path):
    path, config = fixture_config(tmp_path)
    with pytest.raises(ComputeLimit, match="checkpoint"):
        run_campaign(path, "test", NOW, max_new_runs=1)
    root = run_campaign(path, "test", NOW)
    manifest = json.loads((root / "manifest.json").read_text())
    assert any(o["reused"] == 1 for o in manifest["operations"])
    first = json.loads((root / "report-data.json").read_text())
    path.write_text(
        path.read_text()
        .replace("workers = 1", "workers = 2")
        .replace(
            'backtest_results_dir = "backtest_results/backtest"',
            'backtest_results_dir = "parallel"',
        ),
        encoding="utf-8",
    )
    other = run_campaign(path, "test", NOW)
    assert json.loads((other / "report-data.json").read_text()) == first
    assert (other / "report.md").read_text().split("## Execution metadata")[0] == (
        root / "report.md"
    ).read_text().split("## Execution metadata")[0]
    w = config.windows[0]
    job = make_job(
        config, root, manifest, "S1A-S2A", 0, w.train_start, w.train_end, "SUMMARY", D(1) / 12
    )
    assert cached(root, manifest, job) is not None
    entry = manifest["artifacts"][job.key]
    original = (root / entry["path"]).read_bytes()
    (root / entry["path"]).write_text("corrupt")
    assert cached(root, manifest, job) is None
    (root / entry["path"]).write_bytes(original)
    entry["status"] = "INCOMPLETE"
    assert cached(root, manifest, job) is None
    entry["status"] = "COMPLETED"
    assert cached(root, manifest, replace(job, key="unpublished-temporary")) is None
    fulljob = make_job(config, root, manifest, "S1A-S2A", 0, w.train_end, w.end, "FULL", D(1) / 12)
    full = manifest["artifacts"][fulljob.key]
    assert cached(root, manifest, fulljob) is not None
    (root / full["files"][0]["path"]).write_text("corrupt sqlite")
    assert cached(root, manifest, fulljob) is None


def test_conflicts_invalidate_and_summary_has_no_sqlite(tmp_path):
    path, config = fixture_config(tmp_path, conflicts=True)
    root = run_campaign(path, "test", NOW)
    body = json.loads((root / "report-data.json").read_text())
    assert all(a["verdict"] == "INDETERMINATE" for a in body["aggregates"].values())
    assert all(w["status"] == "INVALID_DATA" for rows in body["windows"].values() for w in rows)
    assert not list((root / "artifacts").rglob("*.sqlite3"))


def test_budget_refusal_and_key_separation(tmp_path):
    path, config = fixture_config(tmp_path)
    root = run_campaign(path, "test", NOW, prepare_only=True)
    manifest = json.loads((root / "manifest.json").read_text())
    w = config.windows[0]

    def job(c=config, m=manifest, kind="SUMMARY"):
        return make_job(c, root, m, "S1A-S2A", 0, w.train_start, w.train_end, kind, D(1) / 12)

    original = job()
    assert original.key != job(kind="FULL").key
    assert (
        original.key
        != job(
            c=replace(
                config,
                research=replace(
                    config.research,
                    close_policy=replace(config.research.close_policy, max_close_attempts=4),
                ),
            )
        ).key
    )
    for field in ("data_hash", "code_commit"):
        altered = {**manifest, "identity": {**manifest["identity"], field: "changed"}}
        assert original.key != job(m=altered).key
    assert estimate(D(10), D(84), 4, D(".8"))["work_seconds"] == "840"
    path.write_text(
        path.read_text().replace('max_compute_hours = "6"', 'max_compute_hours = "0.000001"')
    )
    with pytest.raises(ComputeLimit, match="remaining"):
        run_campaign(path, "test", NOW)
    assert not json.loads((root / "manifest.json").read_text())["artifacts"]


def test_training_rank_never_reads_next_test_open(tmp_path):
    from richletsgo.backtest.statistics import summarize
    from richletsgo.marketdata.models import Bar, Interval
    from richletsgo.strategy.models import S1BParams

    active = run_config(24)
    flat = replace(active, s1=S1BParams(1, 1))
    with MarketDataStore(tmp_path / "source") as store:
        populate(store, 24)
        # An old intraday spike blocks the breakout candidate while the moving
        # average candidate stays long into its final-hour liquidation.
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE bars SET high='1000' WHERE start_ms=?", (to_ms(START - 48 * HOUR),))

        def rank():
            results = [
                asyncio.run(
                    r.execute(
                        store,
                        results_dir=tmp_path / "results",
                        journal_dir=tmp_path / "journal",
                        storage_policy="SUMMARY",
                    )
                )
                for r in (active, flat)
            ]
            assert results[0].trades and all(
                t["closed_at"] == (active.end - HOUR).isoformat() for t in results[0].trades
            )
            assert not results[1].trades
            rows = [((D(i),), summarize(r, str(i))) for i, r in enumerate(results)]
            return rows, choose(rows, D(100), {"S1": 0, "S2": 0})

        before = rank()
        for symbol in Symbol:
            store.put_bars(
                [
                    Bar(
                        symbol,
                        Interval.HOUR,
                        active.end,
                        D(99999),
                        D(99999),
                        D(99999),
                        D(99999),
                        D(1),
                        D(99999),
                    )
                ]
            )
        assert rank() == before


def test_no_candidate_skips_full_runs_and_reestimate_preserves_checkpoint(tmp_path):
    path, config = fixture_config(tmp_path, minimum=999999)
    # The initial 84-second estimate fits. Artificially low source timing forces
    # the real first measurement to exceed this budget, without changing results.
    text = path.read_text()
    path.write_text(
        text.replace('per_run_seconds_12m = "84"', 'per_run_seconds_12m = "0.000001"')
        .replace('max_compute_hours = "6"', 'max_compute_hours = "0.000001"')
        .replace("reestimate_after = 4", "reestimate_after = 1")
    )
    with pytest.raises(ComputeLimit, match="remaining"):
        run_campaign(path, "test", NOW)
    root = next((config.research.results_dir / "campaigns").iterdir())
    manifest = json.loads((root / "manifest.json").read_text())
    assert len(manifest["artifacts"]) == 1  # result published before reestimate stops
    path.write_text(text)
    root = run_campaign(path, "test", NOW)
    body = json.loads((root / "report-data.json").read_text())
    assert all(w["status"] == "NO_CANDIDATE" for rows in body["windows"].values() for w in rows)
    assert all(
        a["net_pnl"] == "0" and a["verdict"] == "INDETERMINATE" for a in body["aggregates"].values()
    )
    assert not list((root / "artifacts").rglob("*.sqlite3"))
