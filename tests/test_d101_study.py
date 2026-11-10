"""D101 daily signals, state machine, progression and synthetic-only execution checks."""

import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from d98_fixtures import collected_dataset
from d101_fixtures import hourly_dataset

from richletsgo.backtest.codec import encode
from richletsgo.research.d98.boundaries import DAY, HOUR, STUDY_START, epoch
from richletsgo.research.d98.models import D, FundingPoint, RawBar
from richletsgo.research.d98.store import Store
from richletsgo.research.d98.study import study as d98_study
from richletsgo.research.d101 import execution
from richletsgo.research.d101.daily import daily_bars
from richletsgo.research.d101.models import (
    D98_FREEZE_DIGEST,
    GRID,
    HOLD,
    Combination,
    adjust_stop,
    stop_price,
)
from richletsgo.research.d101.signals import Series
from richletsgo.research.d101.simulate import simulate
from richletsgo.research.d101.statistics import Aggregate, verdict
from richletsgo.research.d101.study import contract_study, study


def raw_bar(at: int, opening: str, high: str, low: str, close: str) -> RawBar:
    return RawBar(at, D(opening), D(high), D(low), D(close), D(1), D(1))


def _daily_row(at: int, direction: int, future_day: int) -> tuple[D, D, D, D]:
    if at == _T - DAY:
        return (D(100), D(112), D(95), D(110)) if direction == 1 else (D(100), D(105), D(88), D(90))
    if at < _T:
        return D(100), D(105), D(95), D(100)
    if direction == 1:
        opening = D(100 + future_day)
        close = opening + 10
        return opening, close + 5, opening, close
    opening = D(100 - future_day)
    close = opening - 5
    return opening, opening, close - 2, close


_T = epoch("2023-01-01")


def trade_series(
    direction: int = 1,
    *,
    days_after: int = 92,
    funding_hours: int = 4,
) -> Series:
    start = _T - 56 * DAY
    end = _T + (days_after + 1) * DAY
    bars = []
    for day in range(start, end, DAY):
        future_day = max(0, (day - _T) // DAY)
        opening, high, low, close = _daily_row(day, direction, future_day)
        for hour in range(24):
            at = day + hour * HOUR
            bar_open = opening if hour == 0 else close
            bars.append(RawBar(at, bar_open, high, low, close, D(1), D(1)))
    funding = [
        FundingPoint(at, D("0.001"))
        for at in range(start - 8 * HOUR, end + 1, funding_hours * HOUR)
    ]
    return Series(bars, funding)


def trade_features(slip: str = "0.001") -> dict[str, object]:
    return {"slippage": D(slip), "liquidity_bucket": "20-50M"}


def test_daily_aggregation_requires_all_24_hours_and_uses_utc_ohlc():
    rows = [
        raw_bar(i * HOUR, str(100 + i), str(110 + i), str(90 + i), str(105 + i)) for i in range(24)
    ]
    daily = daily_bars(rows)
    assert list(daily) == [0]
    assert daily[0] == RawBar(0, D(100), D(133), D(90), D(128), D(24), D(24))
    assert daily_bars(rows[:12] + rows[13:]) == {}
    with pytest.raises(ValueError, match="duplicate"):
        daily_bars(rows + [rows[0]])


@pytest.mark.parametrize("n", [20, 55])
def test_breakout_is_strict_and_excludes_signal_day_high(n):
    series = trade_series()
    at = _T
    b0 = series.daily[at - DAY]
    series.daily[at - DAY] = replace(b0, close=D(105), high=D(999))
    for i in range(2, n + 2):
        bar = series.daily[at - i * DAY]
        series.daily[at - i * DAY] = replace(bar, high=D(105), low=D(95))
    assert series.daily_breakout(at, n) == 0  # equality is not a breakout
    series.daily[at - DAY] = replace(series.daily[at - DAY], close=D(106))
    assert series.daily_breakout(at, n) == 1  # B0.high=999 is not in the channel


def test_n20_and_n55_channel_boundaries():
    series = trade_series()
    at = _T
    for i in range(2, 57):
        bar = series.daily[at - i * DAY]
        series.daily[at - i * DAY] = replace(bar, high=D(105), low=D(95))
    outside_n20 = series.daily[at - 22 * DAY]
    series.daily[at - 22 * DAY] = replace(outside_n20, high=D(200))
    assert series.daily_breakout(at, 20) == 1
    assert series.daily_breakout(at, 55) == 0
    series.daily[at - 22 * DAY] = replace(outside_n20, high=D(105))
    assert series.daily_breakout(at, 55) == 1


def test_d99_mean_uses_the_30_complete_days_before_signal_day():
    series = trade_series()
    at = _T
    for mean, slip in (
        (D("5000000"), D("0.0025")),
        (D("20000000"), D("0.0015")),
        (D("50000000"), D("0.0010")),
        (D("200000000"), D("0.0005")),
    ):
        for j in range(1, 31):
            day = at - (j + 1) * DAY
            series.daily[day] = replace(series.daily[day], turnover=mean)
        features, reason = series.features(at)
        assert reason is None
        assert features["mean_turnover"] == mean
        assert features["slippage"] == slip
    series.daily[at - DAY] = replace(series.daily[at - DAY], turnover=D(0))
    assert series.features(at)[1] is None  # B_0 is excluded from D_1..D_30
    for j in range(1, 31):
        day = at - (j + 1) * DAY
        series.daily[day] = replace(series.daily[day], turnover=D(0))
    assert series.features(at)[1] == "zero_mean_turnover"


def test_56_day_history_window_is_left_inclusive_and_right_exclusive():
    series = trade_series()
    assert series.input_complete(_T)
    rows = list(series.bars.values())
    rows.remove(series.bars[_T - 56 * DAY])
    assert not Series(rows, series.funding).input_complete(_T)


def test_grid_long_only_and_same_time_reverse_signal():
    series = trade_series(direction=-1)
    at = _T + DAY
    series.daily[_T] = replace(series.daily[_T], low=D(75), close=D(80))
    assert series.daily_breakout(at, 20) == -1
    assert series.entry_direction(at, Combination(20, 10, "both")) == -1
    assert series.entry_direction(at, Combination(20, 10, "long")) == 0
    assert [combo.key for combo in GRID] == [
        "N20-M10-both",
        "N20-M10-long",
        "N55-M20-both",
        "N55-M20-long",
    ]


@pytest.mark.parametrize("direction", [1, -1])
def test_trailing_stop_only_moves_favorably(direction):
    if direction == 1:
        assert adjust_stop(D(95), D(100), direction) == D(100)
        assert adjust_stop(D(100), D(97), direction) == D(100)
    else:
        assert adjust_stop(D(105), D(100), direction) == D(100)
        assert adjust_stop(D(100), D(103), direction) == D(100)
    with pytest.raises(ValueError, match="direction"):
        adjust_stop(D(100), D(101), 0)


@pytest.mark.parametrize("direction", [1, -1])
def test_trailing_stop_is_effective_on_update_bar_without_retroactive_touch(direction):
    series = trade_series(direction)
    at = _T + 11 * DAY
    combo = Combination(20, 10, "both")
    candidate = stop_price(series.daily, at, combo.m, direction)
    expected = D(101) if direction == 1 else D(99)
    assert candidate == expected
    previous = D(95) if direction == 1 else D(105)
    assert adjust_stop(previous, candidate, direction) == expected

    bar = series.bars[at]
    series.bars[at] = replace(
        bar,
        low=expected if direction == 1 else bar.low,
        high=expected if direction == -1 else bar.high,
    )
    trade, reason, release = simulate(
        series,
        {"data_end": _T + 93 * DAY, "tail": "not_applicable", "L_a": None},
        "SYNUSDT",
        _T,
        direction,
        combo,
        trade_features(),
        "FLAT",
    )
    assert reason is None and trade is not None
    assert trade["exit_reason"] == "stop"
    assert trade["exit_time"] == at == release
    assert trade["P_x"] == expected * (1 - direction * D("0.001"))


@pytest.mark.parametrize("hours", [1, 2, 4, 8])
@pytest.mark.parametrize("direction", [1, -1])
def test_funding_excludes_entry_and_includes_stop_exit_settlement(hours, direction):
    series = trade_series(direction, funding_hours=hours)
    exit_at = _T + hours * HOUR
    target = series.bars[exit_at]
    series.bars[exit_at] = replace(
        target,
        low=D(95) if direction == 1 else target.low,
        high=D(105) if direction == -1 else target.high,
    )
    trade, reason, _ = simulate(
        series,
        {"data_end": _T + 93 * DAY, "tail": "not_applicable", "L_a": None},
        "SYNUSDT",
        _T,
        direction,
        Combination(20, 10, "both"),
        trade_features(),
        "LONG" if direction == 1 else "SHORT",
    )
    assert reason is None and trade is not None
    assert trade["exit_time"] == exit_at
    assert trade["funding"] == direction * D("0.001") * series.bars[exit_at].open


def test_gap_entry_and_intrabar_gap_stop_prices():
    series = trade_series()
    bar = series.bars[_T]
    series.bars[_T] = replace(bar, open=D(94), high=D(100), low=D(90), close=D(100))
    trade, reason, release = simulate(
        series,
        {"data_end": _T + 93 * DAY, "tail": "not_applicable", "L_a": None},
        "SYNUSDT",
        _T,
        1,
        Combination(20, 10, "both"),
        trade_features(),
        "FLAT",
    )
    assert reason is None and trade is not None
    assert (trade["exit_reason"], trade["exit_time"], release) == ("gap", _T, _T)
    assert trade["P_e"] == D(94) * D("1.001")
    assert trade["P_x"] == D(94) * D("0.999")

    series = trade_series()
    target = series.bars[_T + HOUR]
    series.bars[_T + HOUR] = replace(target, open=D(94), high=D(96), low=D(90), close=D(95))
    trade, reason, _ = simulate(
        series,
        {"data_end": _T + 93 * DAY, "tail": "not_applicable", "L_a": None},
        "SYNUSDT",
        _T,
        1,
        Combination(20, 10, "both"),
        trade_features(),
        "FLAT",
    )
    assert reason is None and trade is not None and trade["exit_reason"] == "gap"
    assert trade["P_x"] == D(94) * D("0.999")


@pytest.mark.parametrize("direction", [1, -1])
def test_h_exit_is_exactly_90_days(direction):
    series = trade_series(direction)
    trade, reason, release = simulate(
        series,
        {"data_end": _T + 93 * DAY, "tail": "not_applicable", "L_a": None},
        "SYNUSDT",
        _T,
        direction,
        Combination(20, 10, "both"),
        trade_features(),
        "FLAT",
    )
    assert reason is None and trade is not None
    assert trade["exit_reason"] == "H"
    assert trade["exit_time"] == release == _T + HOLD


def test_nonpositive_risk_is_invalid():
    series = trade_series()
    for i in range(1, 11):
        at = _T - i * DAY
        series.daily[at] = RawBar(at, D(110), D(110), D(110), D(110), D(0), D(0))
    result, invalid, stop_at = simulate(
        series,
        {"data_end": _T + 93 * DAY, "tail": "not_applicable", "L_a": None},
        "SYNUSDT",
        _T,
        1,
        Combination(20, 10, "both"),
        trade_features(),
        "FLAT",
    )
    assert result is None and invalid == "nonpositive_risk" and stop_at == _T


def test_missing_future_hour_and_funding_gap_release_at_first_missing_point():
    series = trade_series()
    missing = _T + 5 * HOUR
    series.bars.pop(missing)
    trade, reason, release = simulate(
        series,
        {"data_end": _T + 93 * DAY, "tail": "not_applicable", "L_a": None},
        "SYNUSDT",
        _T,
        1,
        Combination(20, 10, "both"),
        trade_features(),
        "FLAT",
    )
    assert trade is None and reason == "future_bars" and release == missing

    series = trade_series()
    series.funding = [FundingPoint(_T, D("0.001"))]
    series.funding_times = [_T]
    trade, reason, release = simulate(
        series,
        {"data_end": _T + 93 * DAY, "tail": "not_applicable", "L_a": None},
        "SYNUSDT",
        _T,
        1,
        Combination(20, 10, "both"),
        trade_features(),
        "FLAT",
    )
    assert trade is None and reason == "future_funding" and release == _T + 9 * HOUR


@pytest.mark.parametrize("direction", [1, -1])
def test_delist_ea_uses_last_close_two_percent_and_ea_funding(direction):
    series = trade_series(direction, funding_hours=4)
    end = _T + 8 * HOUR
    last = series.bars[end - HOUR]
    trade, reason, release = simulate(
        series,
        {"data_end": end, "tail": "allowed", "L_a": end - HOUR},
        "SYNUSDT",
        _T,
        direction,
        Combination(20, 10, "both"),
        trade_features(),
        "FLAT",
    )
    assert reason is None and trade is not None
    assert trade["exit_reason"] == "delist" and trade["exit_time"] == release == end
    assert trade["P_x"] == last.close * (1 - direction * D("0.02"))
    assert trade["funding"] == direction * D("0.001") * (
        series.bars[_T + 4 * HOUR].open + last.close
    )


def _stat_row(at: int, r: str, symbol: str = "SYNUSDT") -> dict[str, object]:
    value = D(r)
    return {
        "t": at,
        "symbol": symbol,
        "d": 1,
        "R": value,
        "R0": D(1),
        "pnl": value,
        "fees": D(0),
        "funding": D(0),
        "slippage_cost": D(0),
        "liquidity_bucket": ">=200M",
        "exit_reason": "H",
        "btc_state": "FLAT",
        "holding_hours": 24,
    }


def test_progression_thresholds_exact_mean_majority_and_bundle_mean():
    agg = Aggregate(300, {})
    rows = []
    for year in range(2022, 2027):
        value = "0.5" if year <= 2024 else "-0.5"
        for index in range(60):
            rows.append(_stat_row(epoch(f"{year}-01-01") + index * DAY, value))
    for row in rows:
        agg.add(row)
    result = agg.result()
    assert result["mean_R"] == D("0.10")
    assert result["criteria_1_4"] == [True, True, True, True]
    assert result["candidate"] is True

    zero_trim = Aggregate(300, {})
    for index in range(297):
        zero_trim.add(_stat_row(epoch("2023-01-01") + index * DAY, "0"))
    for index in range(3):
        zero_trim.add(_stat_row(epoch("2024-01-01") + index * DAY, "1", f"TOP{index}"))
    assert zero_trim.result()["trimmed_mean_R"] == 0
    assert zero_trim.result()["criteria_1_4"][1] is False


def test_top_one_percent_heap_keeps_the_final_count_of_highest_results():
    agg = Aggregate(101, {})
    start = epoch("2024-01-01")
    for index, value in enumerate([*range(100, 0, -1), 0]):
        agg.add(_stat_row(start + index * DAY, str(value), f"SYN{index:03d}USDT"))

    result = agg.result()
    assert result["trim_count"] == 2
    assert result["trimmed_mean_R"] == D("49")


def test_progression_invalid_ratio_boundary_and_date_bundle_zero():
    exact = Aggregate(300, {"future_bars": 15})
    for year in range(2022, 2027):
        for index in range(57):
            exact.add(_stat_row(epoch(f"{year}-01-01") + index * DAY, "0.2"))
    assert exact.result()["undecidable"] is False  # 15/300 = exactly 5%
    over = Aggregate(300, {"future_bars": 16})
    for index in range(284):
        over.add(_stat_row(epoch("2022-01-01") + index * DAY, "0.2"))
    assert over.result()["undecidable"] is True

    bundled = Aggregate(300, {})
    same_day = epoch("2024-01-01")
    for index in range(299):
        bundled.add(_stat_row(same_day, "1", f"SYN{index:03d}USDT"))
    bundled.add(_stat_row(same_day + DAY, "-1", "LASTUSDT"))
    result = bundled.result()
    assert result["mean_R"] > 0
    assert result["entry_date_bundle_mean_R"] == 0
    assert result["criteria_1_4"][3] is False


def test_four_combination_verdict_requires_two_positive_and_candidate():
    names = [combo.key for combo in GRID]
    rows = {name: {"candidate": False, "mean_R_positive": False} for name in names}
    rows[names[0]] = {"candidate": True, "mean_R_positive": True}
    rows[names[1]] = {"candidate": False, "mean_R_positive": True}
    assert verdict(rows, 100, 5) == "GO"
    rows[names[1]]["mean_R_positive"] = False
    assert verdict(rows, 100, 5) == "NO_GO"
    assert verdict(rows, 100, 6) == "STUDY_UNDECIDABLE"


def test_stage_counts_are_independent_of_holds_and_reversal_can_reenter_same_t(
    tmp_path, monkeypatch
):
    path = tmp_path / "synth.sqlite"
    pool, audit = hourly_dataset(path, reverse_on_day_two=False)
    coverage = audit["body"]["contracts"][0]

    class FlatBTC:
        def at(self, at: int) -> int:
            return 0

    row = contract_study(
        (
            path,
            pool["body"]["contracts"][0],
            [pool["body"]["contracts"][0]],
            coverage,
            FlatBTC(),
            tmp_path / "single.jsonl",
        )
    )
    assert row["stages"]["opportunities"] == 3
    assert row["stages"]["metadata_eligible"] == 3
    assert row["stages"]["data_eligible"] == 3
    assert {value["fired"] for value in row["combinations"].values()} == {1}

    path2 = tmp_path / "reverse.sqlite"
    pool2, audit2 = hourly_dataset(path2, reverse_on_day_two=False)
    from richletsgo.research.d101.signals import Series as D101Series

    def forced_breakout(series, at, n):
        del series, n
        return 1 if at == STUDY_START else -1 if at == STUDY_START + DAY else 0

    # Isolate the state-machine transition from the already-tested channel arithmetic.
    monkeypatch.setattr(D101Series, "daily_breakout", forced_breakout)
    result = study(
        pool2,
        audit2,
        path2,
        tmp_path / "study",
        workers=1,
        registration_digest="synthetic-registration",
    )
    assert result["body"]["stages"]["metadata_eligible"] == 3
    assert result["body"]["combinations"]["N20-M10-both"]["signals"] == 2
    assert result["body"]["combinations"]["N20-M10-long"]["signals"] == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "study" / "D101-study-trades.jsonl").read_text().splitlines()
    ]
    both = [row for row in rows if row["combination"] == "N20-M10-both"]
    assert both[0]["exit_reason"] == "reverse"
    assert both[0]["exit_time"] == both[1]["t"] == STUDY_START + DAY


def test_workers_have_identical_body_and_artifact_bytes(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    pool, audit = hourly_dataset(path, reverse_on_day_two=True, judgment=True)
    one = study(pool, audit, path, tmp_path / "one", workers=1, registration_digest="synthetic")
    many = study(pool, audit, path, tmp_path / "many", workers=2, registration_digest="synthetic")
    assert one["sha256"] == many["sha256"]
    for name in ("D101-study.json", "D101-study.md", "D101-study-trades.jsonl"):
        assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "many" / name).read_bytes()
        assert result_is_lf(tmp_path / "one" / name)
        assert result_is_lf(tmp_path / "many" / name)
    trades = (tmp_path / "one" / "D101-study-trades.jsonl").read_text(encoding="utf-8")
    assert "POISONUSDT" not in trades
    assert all(json.loads(line)["symbol"] == "SYNUSDT" for line in trades.splitlines())


def result_is_lf(path) -> bool:
    data = path.read_bytes()
    return b"\r\n" not in data and data.endswith(b"\n")


def test_existing_d98_synthetic_result_digest_is_unchanged(tmp_path):
    path = tmp_path / "d98-synthetic.sqlite"
    pool, audit = collected_dataset(path)
    result = d98_study(
        pool,
        audit,
        path,
        tmp_path / "d98-result",
        workers=1,
        registration_digest="synthetic",
    )
    assert result["sha256"] == "4763e050b2babf362015f9379c5ec4e596bb1a670ced4e7eff02e1d6aa68ddc7"
    assert result["body"]["trades"]["sha256"] == (
        "38ee18b53cb28e93e1e67ca75685f721e1a2a00202410e3b51382bd30dc798cf"
    )
    assert result["body"]["trades"]["count"] == 8


def test_frozen_dataset_is_read_only_and_hash_recalculation_is_synthetic(tmp_path):
    path = tmp_path / "readonly.sqlite"
    pool, audit = hourly_dataset(path)
    recomputed = execution.verify_frozen_dataset(
        pool,
        audit,
        path,
        expected_pool_digest=pool["sha256"],
        expected_data_hash=audit["body"]["data_hash"],
    )
    assert recomputed["body"]["data_hash"] == audit["body"]["data_hash"]
    store = Store(path, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.db.execute("UPDATE identity SET value='changed' WHERE key='frozen'")
    finally:
        store.close()
    store = Store(path, readonly=True)
    try:
        assert (
            store.db.execute("SELECT value FROM identity WHERE key='frozen'").fetchone()[0]
            == D98_FREEZE_DIGEST
        )
    finally:
        store.close()


def test_wrong_freeze_rejects_before_audit_recalculation(tmp_path, monkeypatch):
    path = tmp_path / "wrong-freeze.sqlite"
    pool, audit = hourly_dataset(path, freeze="wrong-registration")
    monkeypatch.setattr(execution, "_audit", lambda *args, **kwargs: pytest.fail("audit ran"))
    with pytest.raises(ValueError, match="freeze"):
        execution.verify_frozen_dataset(
            pool,
            audit,
            path,
            expected_pool_digest=pool["sha256"],
            expected_data_hash=audit["body"]["data_hash"],
        )


def test_checkout_c_uses_later_published_registration_inputs(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    checkout_c = tmp_path / "checkout-c"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Synthetic Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "synthetic@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "false"], check=True)

    reports = repo / "docs" / "reports"
    reports.mkdir(parents=True)
    freeze = execution.envelope({"synthetic": "freeze"})
    monkeypatch.setattr(execution, "D98_FREEZE_DIGEST", freeze["sha256"])
    freeze_path = reports / "D98-study-preregistration.json"
    freeze_path.write_text(encode(freeze) + "\n", encoding="utf-8", newline="\n")
    (repo / "README.md").write_text("synthetic C\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "C"], check=True, capture_output=True)
    code_commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", code_commit],
        check=True,
    )

    pool_body = {"synthetic": "pool"}
    pool = execution.envelope(pool_body)
    audit = execution.envelope(
        {
            "pool_sha256": pool["sha256"],
            "data_hash": "synthetic-data-hash",
            "status": "READY",
            "contracts": [],
        }
    )
    monkeypatch.setattr(execution, "D98_POOL_DIGEST", pool["sha256"])
    monkeypatch.setattr(execution, "D98_AUDIT_DIGEST", audit["sha256"])
    monkeypatch.setattr(execution, "D98_DATA_HASH", audit["body"]["data_hash"])
    reg = execution.registration(code_commit, pool, audit)
    input_values = {
        execution.REGISTRATION_PATH: reg,
        execution.POOL_PATH: pool,
        execution.AUDIT_PATH: audit,
    }
    input_files: dict[str, Path] = {}
    for relative, value in input_values.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encode(value) + "\n", encoding="utf-8", newline="\n")
        input_files[relative] = path
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "publish synthetic registration"],
        check=True,
        capture_output=True,
    )
    published_commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", published_commit],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(checkout_c), code_commit],
        check=True,
        capture_output=True,
    )

    execution.validate_checkout(
        checkout_c,
        reg,
        pool,
        audit,
        registration_file=input_files[execution.REGISTRATION_PATH],
        pool_file=input_files[execution.POOL_PATH],
        audit_file=input_files[execution.AUDIT_PATH],
    )


def test_mutated_synthetic_database_fails_data_hash_before_study(tmp_path):
    path = tmp_path / "changed.sqlite"
    pool, audit = hourly_dataset(path)
    store = Store(path)
    try:
        store.db.execute(
            "UPDATE bars_1h SET close='121' WHERE symbol=? AND start=?",
            (pool["body"]["contracts"][0]["symbol"], STUDY_START),
        )
        store.db.commit()
    finally:
        store.close()
    with pytest.raises(ValueError, match="data_hash"):
        execution.verify_frozen_dataset(
            pool,
            audit,
            path,
            expected_pool_digest=pool["sha256"],
            expected_data_hash=audit["body"]["data_hash"],
        )
