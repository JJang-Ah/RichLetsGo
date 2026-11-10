"""Fixed D101 study policy and its four registered combinations."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from richletsgo.research.d98.boundaries import DAY, END, STUDY_START, epoch
from richletsgo.research.d98.models import FEE, D, RawBar, calculated

HOUR = 3_600_000
HOLD = 90 * DAY
SIGNAL_END = epoch("2026-07-01")
FUNDING_GAP = 8 * HOUR
MIN_HISTORY = 56 * DAY
MIN_LISTING_AGE = 60 * DAY
D98_FREEZE_DIGEST = "f7388c0737eafecee0ead281c8aea486c3155ba9c32303742a53273b3106d2e3"
D98_POOL_DIGEST = "8e5e2d0c4dfa32e0269dd9c906203a97026f552fab739bbe90c68d5368e206fe"
D98_AUDIT_DIGEST = "dace1ad6783af04e31209a9bcd95911e9a585852d5518af175c44ee9bd667685"
D98_DATA_HASH = "42acf4dfb41f5d9eff49c02c73a37cc5bc8d6c3d48de04b42382d0b04a08bc6c"
POLICY_VERSION = "D101-study-v1"

Mode = Literal["both", "long"]


@dataclass(frozen=True, slots=True, order=True)
class Combination:
    n: int
    m: int
    mode: Mode

    @property
    def key(self) -> str:
        return f"N{self.n}-M{self.m}-{self.mode}"


GRID = tuple(Combination(n, m, mode) for n, m in ((20, 10), (55, 20)) for mode in ("both", "long"))

POLICY = {
    "version": POLICY_VERSION,
    "signal_window": [STUDY_START, SIGNAL_END],
    "signal_window_end_exclusive": True,
    "data_end": END,
    "grid": [[c.n, c.m, c.mode] for c in GRID],
    "holding_hours": HOLD // HOUR,
    "fee": format(FEE, "f"),
    "slippage": [
        ["200000000", "0.0005"],
        ["50000000", "0.0010"],
        ["20000000", "0.0015"],
        ["5000000", "0.0025"],
    ],
    "rules": {
        "daily": "UTC daily bars are aggregated from exactly 24 complete 1h bars.",
        "breakout": (
            "LONG close(B_0) > max(high(B_1..B_N)); SHORT close(B_0) < "
            "min(low(B_1..B_N)); strict comparison, signal day excluded."
        ),
        "entry": "At t UTC 00:00, use the 1h open with adverse D99 slippage.",
        "initial_stop": "LONG min(low(B_0..B_(M-1))); SHORT max(high(B_0..B_(M-1))).",
        "risk": "R0 = d * (close(B_0) - S0); R0 <= 0 is invalid.",
        "trail": "At UTC 00:00, update from the last M closed daily bars; effective this 1h bar.",
        "liquidity": "mean(D_1..D_30); D_j is the completed 24h window before B_0 per 04 3.3.",
        "future_path": "Require complete hourly bars through the exit bar and funding gaps <= 8h.",
        "funding": "Apply d * rate * 1h open for t < tau <= exit; delist tau=E_a uses close(L_a).",
        "delist": "At E_a, close at close(L_a) * (1 - d * 0.02); this replaces D99 slippage.",
        "exit_time": "Start timestamp of the exit 1h bar; delisting liquidation uses E_a.",
        "btc_state": "Recorded at entry for technical reporting only; not an entry filter.",
        "invalid": "Stop at the first missing future point, omit R, and resume from the next t.",
    },
    "criteria": {
        "min_valid": 300,
        "mean_r": "0.10",
        "trim_top_fraction": "0.01",
        "trim_ties": ["t ascending", "symbol ascending"],
        "min_year_valid": 30,
        "min_years": 3,
        "positive_years_strict_majority": True,
        "entry_date_bundle_mean_strictly_positive": True,
        "positive_combinations": 2,
        "combination_denominator": 4,
        "invalid_ratio_max": "0.05",
        "input_insufficient_ratio_max": "0.05",
    },
    "limitations": [
        "Same-day breakouts across symbols are correlated, not independent trade samples; "
        "criteria 3 and 4 partially mitigate this limitation.",
        "Long holdings in low-liquidity symbols face risk-tier and leverage constraints "
        "beyond tiered slippage; these are addressed in stage 2.",
        "DESIGN pool only; this is a research gate, not holdout validation.",
        "Independent one-unit trades; no portfolio, liquidation or capacity model.",
        "Daily candles are aggregated from UTC hourly data.",
        "D101 S1-B and the long-only variant were selected after prior DESIGN results.",
    ],
}


@calculated
def stop_price(daily: dict[int, RawBar], at: int, m: int, direction: int) -> Decimal:
    """Initial/trailing channel extreme for M closed UTC daily bars ending at ``at``."""
    bars = [daily.get(at - i * DAY) for i in range(1, m + 1)]
    if any(bar is None for bar in bars):
        raise ValueError("daily input missing")
    present = [bar for bar in bars if bar is not None]
    return min(b.low for b in present) if direction == 1 else max(b.high for b in present)


def adjust_stop(current: Decimal, candidate: Decimal, direction: int) -> Decimal:
    if direction == 1:
        return max(current, candidate)
    if direction == -1:
        return min(current, candidate)
    raise ValueError("direction must be +/-1")


def liquidity_bucket(mean: Decimal) -> str:
    if mean >= D("200000000"):
        return ">=200M"
    if mean >= D("50000000"):
        return "50-200M"
    if mean >= D("20000000"):
        return "20-50M"
    if mean >= D("5000000"):
        return "5-20M"
    return "<5M"
