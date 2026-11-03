"""Decimal-only, symbol-independent D98 inputs and fixed study policy."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from functools import wraps

from .boundaries import HOUR

D = Decimal
CALC = Context(prec=34)


def calculated[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with localcontext(CALC):
            return fn(*args, **kwargs)

    return wrapped


@dataclass(frozen=True, slots=True)
class RawBar:
    start: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal

    def __post_init__(self) -> None:
        values = (self.open, self.high, self.low, self.close, self.volume, self.turnover)
        if type(self.start) is not int or any(
            not isinstance(v, Decimal) or not v.is_finite() for v in values
        ):
            raise ValueError("finite Decimal bar and integer timestamp required")
        if (
            min(self.open, self.high, self.low, self.close) <= 0
            or min(self.volume, self.turnover) < 0
            or self.low > min(self.open, self.close)
            or self.high < max(self.open, self.close)
        ):
            raise ValueError("invalid OHLCV")


@dataclass(frozen=True, slots=True)
class FundingPoint:
    time: int
    rate: Decimal

    def __post_init__(self) -> None:
        if (
            type(self.time) is not int
            or not isinstance(self.rate, Decimal)
            or not self.rate.is_finite()
        ):
            raise ValueError("invalid funding")


@dataclass(frozen=True, slots=True, order=True)
class Combination:
    v: int
    n: int
    k: int

    @property
    def key(self) -> str:
        return f"v{self.v}-N{self.n}-k{self.k}"


GRID = tuple(Combination(v, n, k) for v in (2, 3) for n in (42, 84) for k in (2, 3))
FEE = D("0.00055")
FUNDING_LIMIT = D("0.0005")
HOLD = 168 * HOUR
POLICY = {
    "version": "D98-study-v1",
    "grid": [[g.v, g.n, g.k] for g in GRID],
    "e": 3,
    "f": "0.0005",
    "H": 42,
    "fee": "0.00055",
    "liquidity_slippage": [
        ["200000000", "0.0005"],
        ["50000000", "0.0010"],
        ["20000000", "0.0015"],
        ["5000000", "0.0025"],
    ],
    "criteria": {
        "min_signals": 300,
        "mean_r": "0.10",
        "trim_top": "0.01",
        "trimmed_mean_strictly_positive": True,
        "min_year_signals": 30,
        "min_years": 3,
        "positive_years_strict_majority": True,
        "positive_combinations": 4,
        "denominator": 8,
        "invalid_ratio_max": "0.05",
    },
    "limitations": [
        "metadata list may omit historic contracts",
        "regularly missing funding records may masquerade as a longer interval",
        "independent one-unit trades; no portfolio, liquidation or capacity model",
        "funding after modelled delist Ea is ignored",
        "design reference only; not holdout validation",
    ],
}
