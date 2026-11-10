"""Small synthetic SQLite integration, byte stability and concentration gates."""

import json

import pytest
from d98_fixtures import contract, make_pool

from richletsgo.backtest.codec import digest, encode
from richletsgo.research.d98.boundaries import DAY, HOUR, START, STUDY_START
from richletsgo.research.d98.models import D
from richletsgo.research.d98.store import Store
from richletsgo.research.d102.execution import _completed
from richletsgo.research.d102.statistics import summarize, verdict
from richletsgo.research.d102.study import markdown, study


def dataset(path):
    end = STUDY_START + 8 * DAY
    contracts = [contract(f"S{i:02}USDT", end=end, key=f"S{i:02}") for i in range(25)]
    poison = contract("POISONUSDT", end=end, key="POISON")
    poison["pool"] = "JUDGMENT"
    pool = make_pool([*contracts, poison])
    store = Store(path)
    try:
        with store.db:
            for index, c in enumerate(contracts):
                store.put(
                    "bars",
                    c["symbol"],
                    (
                        [t, "100", "100", "100", "100", "1", "1000000"]
                        for t in range(START, end, HOUR)
                    ),
                )
                store.put(
                    "funding",
                    c["symbol"],
                    ([t, str(D(index) / 10000)] for t in range(START - DAY, end + 1, 8 * HOUR)),
                )
            store.db.execute(
                "INSERT INTO bars_1h VALUES (?,?,?,?,?,?,?,?)",
                ("POISONUSDT", START, "bad", "bad", "bad", "bad", "bad", "bad"),
            )
        store.freeze("synthetic")
    finally:
        store.close()
    body = {
        "status": "READY",
        "pool_sha256": pool["sha256"],
        "data_hash": "synthetic",
        "contracts": [
            {"symbol": c["symbol"], "data_end": end, "tail": "allowed", "L_a": end - HOUR}
            for c in contracts
        ],
    }
    return pool, {"body": body, "sha256": digest(body)}


def test_worker_digest_lf_completed_recall_and_no_judgment_read(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    pool, audit = dataset(path)
    one = study(pool, audit, path, tmp_path / "one", workers=1, registration_digest="synthetic")
    many = study(pool, audit, path, tmp_path / "many", workers=2, registration_digest="synthetic")
    assert one["sha256"] == many["sha256"]
    assert one["body"]["positions"]["count"] > 0
    assert one["body"]["stages"]["data_eligible"] == 25 * 8
    for name in (
        "D102-study.json",
        "D102-study.md",
        "D102-study-periods.jsonl",
        "D102-study-positions.jsonl",
    ):
        raw = (tmp_path / "one" / name).read_bytes()
        assert b"\r" not in raw
        assert raw == (tmp_path / "many" / name).read_bytes()
    assert markdown(one) == markdown(json.loads(encode(one)))
    assert _completed(tmp_path / "one", "synthetic")["sha256"] == one["sha256"]
    file = tmp_path / "one" / "D102-study-positions.jsonl"
    file.write_bytes(file.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="stream digest"):
        _completed(tmp_path / "one", "synthetic")


def test_same_asset_uses_only_priority_contract(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    pool, audit = dataset(path)
    # Identical key at the same t: second contract must disappear from daily counts
    # and candidates, leaving n=24 and no basket.
    pool["body"]["contracts"][1]["key"] = pool["body"]["contracts"][0]["key"]
    pool["sha256"] = digest(pool["body"])
    audit["body"]["pool_sha256"] = pool["sha256"]
    audit["sha256"] = digest(audit["body"])
    result = study(
        pool, audit, path, tmp_path / "output", workers=1, registration_digest="synthetic"
    )
    assert result["body"]["stages"]["data_eligible"] == 24 * 8
    assert result["body"]["positions"]["count"] == 0


def period(t, value, **changes):
    return {
        "t": t,
        "kind": "normal",
        "net_return": D(value),
        "position_count": 20,
        "invalid_count": 0,
        "btc_return": None,
        "price": D(value),
        "funding": D(0),
        "cost": D(0),
        "turnover": D(0),
        "long_contribution": D(value),
        "short_contribution": D(0),
        **changes,
    }


def test_zero_sd_minimum_n_and_contribution_denominator_including_cash():
    rows = [period(STUDY_START + i * DAY, ".01") for i in range(100)]
    report = summarize(rows, [(D(".02"), STUDY_START, "AB"), (D(".02"), STUDY_START, "ABC")])
    assert report["criteria_1_5"][0] and report["t_statistic"] is None
    assert not summarize(rows[:99], [])["criteria_1_5"][0]
    assert not summarize(rows[:1], [])["criteria_1_5"][0]
    assert report["removed_contributions"][0]["symbol"] == "AB"
    assert report["contribution_trimmed_mean"] == D(".0098")
    rows[0] = period(STUDY_START, "-.01", kind="cash", position_count=0)
    report = summarize(rows, [(D(".02"), STUDY_START + DAY, "AB")])
    assert report["valid"] == 100
    assert report["contribution_trimmed_mean"] == D(".0096")


def test_trim_ceil_tie_order_initial_peak_bankruptcy_and_five_percent():
    rows = [period(STUDY_START + i * DAY, ".01") for i in range(101)]
    report = summarize(rows, [])
    assert report["trim_count"] == 2 and report["trimmed_periods"] == [
        STUDY_START,
        STUDY_START + DAY,
    ]
    assert not summarize([period(STUDY_START, "-.25")], [])["criteria_1_5"][3]
    report = summarize([period(STUDY_START, "-1.1"), period(STUDY_START + DAY, "99")], [])
    assert report["max_drawdown"] == 1 and report["final_equity"] == 0
    assert report["bankrupt_at"] == STUDY_START
    rows = [period(STUDY_START + i * DAY, ".01", invalid_count=1) for i in range(100)]
    assert not summarize(rows, [])["undecidable"]
    rows[0]["invalid_count"] = 2
    assert summarize(rows, [])["undecidable"]
    rows = [
        period(STUDY_START + i * DAY, "0", kind="cash" if i < 5 else "normal") for i in range(100)
    ]
    assert not summarize(rows, [])["undecidable"]
    rows[5]["kind"] = "invalid"
    rows[5]["net_return"] = None
    assert summarize(rows, [])["undecidable"]


def test_fixed_four_combination_and_input_ratio_gate():
    combinations = {str(i): {"candidate": i == 0, "mean_positive": i < 2} for i in range(4)}
    assert verdict(combinations, 100, 5) == "GO"
    assert verdict(combinations, 100, 6) == "STUDY_UNDECIDABLE"
    combinations["1"]["mean_positive"] = False
    assert verdict(combinations, 100, 0) == "NO_GO"


def test_t_exact_two_and_year_count_strict_majority():
    from richletsgo.research.d98.boundaries import epoch

    rows = [period(STUDY_START + i * DAY, "3") for i in range(100)]
    rows[-1]["net_return"] = D(-97)
    result = summarize(rows, [])
    assert result["t_statistic"] == 2 and result["criteria_1_5"][0]
    rows[-1]["net_return"] = D("-97.01")
    assert not summarize(rows, [])["criteria_1_5"][0]
    years = [
        period(epoch(f"{year}-01-01") + i * DAY, ".01" if year < 2024 else "-.01")
        for year in range(2022, 2025)
        for i in range(20)
    ]
    assert summarize(years, [])["criteria_1_5"][2]
    assert not summarize(years[:-1], [])["criteria_1_5"][2]
    years.extend(period(epoch("2025-01-01") + i * DAY, "-.01") for i in range(20))
    assert not summarize(years, [])["criteria_1_5"][2]
