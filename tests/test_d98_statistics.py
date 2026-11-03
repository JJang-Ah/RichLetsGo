"""Fixed D98 criteria, denominators, trimming and deterministic tie breaking."""

from richletsgo.research.d98.boundaries import epoch
from richletsgo.research.d98.models import GRID
from richletsgo.research.d98.statistics import Aggregate, trim_key, verdict


def row(value="0.2", year=2022, symbol="SYN", t=None):
    return dict(
        R=value,
        t=epoch(f"{year}-01-01") if t is None else t,
        symbol=symbol,
        d=1,
        slippage="0.0015",
        exit_reason="H",
        forward_returns={"1": None, "3": "0", "7": "0.1"},
    )


def aggregate(n, years=3, value="0.2", invalid=0):
    a = Aggregate(n)
    for i in range(n):
        a.add(row(value, 2022 + i % years))
    return a.result(n + invalid, {"missing": invalid} if invalid else {})


def test_exact_five_percent_invalid_zero_and_years():
    assert not aggregate(380, invalid=20)["undecidable"]
    bad = aggregate(379, invalid=21)
    assert bad["undecidable"] and not bad["candidate"] and not bad["criterion_4_positive"]
    zero = aggregate(0)
    assert zero["mean_R"] is None and not zero["criterion_4_positive"]
    assert not aggregate(300, years=2)["criteria_1_3"][2]
    assert not aggregate(87, years=3)["criteria_1_3"][2]
    assert aggregate(300)["candidate"]
    assert aggregate(300, value="0.10")["criteria_1_3"][0]
    assert not aggregate(300, value="0.099")["criteria_1_3"][0]
    assert not aggregate(300, value="0")["criteria_1_3"][1]


def test_trim_ceil_ties_and_forward_na():
    a = Aggregate(101)
    rows = [
        row("1", symbol=s, t=epoch("2022-01-01") + i)
        for i, s in [(0, "A"), (0, "AA"), (0, "B"), (1, "A")]
    ]
    for r in rows + [row("0") for _ in range(97)]:
        a.add(r)
    assert sorted(a.top, reverse=True) == [trim_key(rows[0]), trim_key(rows[1])]
    result = a.result(101, {})
    assert result["trim_count"] == 2
    assert result["trimmed_mean_R"] > 0
    assert result["forward_returns"]["1"]["NA"] == 101
    assert result["forward_returns"]["1"]["count"] == 0
    one = aggregate(1)
    assert one["trimmed_mean_R"] is None


def test_same_combination_must_meet_all_criteria_and_fixed_denominator():
    # A meets 1/2 only; B meets 3 only. Cannot combine their passes.
    results = {g.key: aggregate(300, years=2) for g in GRID}
    results[GRID[0].key] = aggregate(90, years=3)
    assert results[GRID[0].key]["criteria_1_3"] == [False, True, True]
    assert verdict(results, 100, 5) == "NO_GO"
    results[GRID[0].key] = aggregate(300)
    assert verdict(results, 100, 5) == "GO"
    assert verdict(results, 100, 6) == "STUDY_UNDECIDABLE"
    for g in GRID[3:]:
        results[g.key] = aggregate(0)
    assert verdict(results, 100, 0) == "NO_GO"
    results[GRID[3].key] = aggregate(300)
    assert verdict(results, 100, 0) == "GO"
    results[GRID[3].key] = aggregate(300, invalid=100)
    assert verdict(results, 100, 0) == "NO_GO"


def test_positive_years_require_strict_majority():
    a = Aggregate(400)
    for year in range(2022, 2026):
        for _ in range(100):
            a.add(row("1" if year < 2024 else "-0.1", year))
    assert not a.result(400, {})["criteria_1_3"][2]
