"""Registered D102 constants. Changes require a new registration."""

from richletsgo.research.d98.boundaries import STUDY_END, STUDY_START

GRID = ((1, 3), (1, 7), (7, 3), (7, 7))
POLICY_VERSION = "D102-study-v1"
POLICY = {
    "version": POLICY_VERSION,
    "grid_days": [list(combo) for combo in GRID],
    "start": STUDY_START,
    "end": STUDY_END,
    "fee": "0.00055",
    "minimum_turnover": "20000000",
    "minimum_candidates": 25,
    "leg_fraction": "0.2",
    "leg_notional": "0.5",
    "slippage": [["200000000", "0.0005"], ["50000000", "0.0010"], ["20000000", "0.0015"]],
    "rules": {
        "grid": "t=start+j*H days, t+H<=end; eligibility counted daily on [start,end).",
        "eligibility": "Active DESIGN contract, launch age>=60d, complete [t-31d,t) hourly bars, "
        "at least one funding in (t-7d,t]. Reasons: zero_mean, liquidity, funding_coverage.",
        "liquidity": "mean(D_1..D_30), D_j=[t-(j+1)d,t-j*d), >=20M inclusive.",
        "score": "Mean(rate*8h/preceding interval) over (t-L,t]; predecessor required, "
        "interval in {1,2,4,8}h, last settlement age<=8h. Both L=1/7 must pass.",
        "basket": "Sort (-F,symbol), first floor(n/5) short, last floor(n/5) long, "
        "signed weights +/-0.5/k; n<25 is cash, close all previous holdings.",
        "price": "g=d*(O(e)/O(t)-1), fixed entry notional; ignore weight drift.",
        "funding": "f=-d*sum(rate*O(tau)/O(t)), t<tau<=e; preceding settlement and "
        "all consecutive intervals and final endpoint must be within 8h.",
        "delist": "Allowed D98 tail, Ea<=e: exit at close(La)*(1-d*0.02); "
        "tau=Ea uses close(La), later funding ignored; fee=w*c, remaining holding zero.",
        "invalid": "Missing entry/exit/funding price or funding gap: contribution zero, "
        "no redistribution, costs and target retained unless delisted. Entire leg invalid "
        "makes period invalid, excluded from statistics but retained in invalid counters.",
        "cost": "sum(abs(a_new-b_prev)*(c+s)); s=current eligible tier or last eligible "
        "rebalance tier, charged to the period starting at t. b is prior target with "
        "delisted symbols zeroed. Final liquidation uses surviving b and t_last tiers.",
        "cash": "Cash return=-close cost; included in all period statistics.",
    },
    "criteria": {
        "min_periods": 100,
        "mean_positive": True,
        "t_min": "2.0",
        "sample_variance": "n-1",
        "zero_sd": "t=null; positive mean passes t gate; n>=100 still required",
        "trim_fraction": "0.01",
        "trim_period_ties": "time ascending",
        "year_min_periods": 20,
        "min_years": 3,
        "positive_years": "strict majority",
        "drawdown_max_exclusive": "0.25",
        "initial_equity": "1",
        "bankruptcy": "r<=-1: equity frozen at zero, drawdown=1, report time",
        "contribution_trim": "Top ceil(.01*N) valid symbol-period contributions in normal "
        "periods; ties time then symbol ascending. Subtract from original period returns, "
        "keep costs, cash periods, weights and period denominator; no redistribution.",
        "positive_combinations": 2,
        "denominator": 4,
        "input_missing_max": "0.05",
        "invalid_positions_max": "0.05",
        "cash_or_invalid_periods_max": "0.05",
    },
    "limitations": [
        "DESIGN pool already used for D98 and D101; not holdout validation.",
        "No weight drift; turnover understated; no liquidation, risk tiers or ADL model.",
        "Independent-period t statistic ignores serial correlation.",
        "Invalid outcomes use future completeness, zero contribution without redistribution; "
        "entire invalid periods and their costs are excluded, subject to the 5% ceiling.",
        "Settlement history, not forecast funding, is used.",
        "All symbols execute at the same hourly opening timestamp.",
    ],
}


def combo_key(lookback: int, holding: int) -> str:
    return f"L{lookback}-H{holding}"
