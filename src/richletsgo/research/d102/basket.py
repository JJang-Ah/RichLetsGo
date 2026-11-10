"""Signed basket targets and stateful turnover costs from 04 section 3.5."""

from dataclasses import dataclass
from decimal import Decimal

from richletsgo.research.d98.models import FEE, D, calculated


@dataclass(frozen=True)
class Candidate:
    symbol: str
    score: Decimal
    slippage: Decimal


@calculated
def targets(candidates: list[Candidate]) -> dict[str, Decimal]:
    if len({row.symbol for row in candidates}) != len(candidates):
        raise ValueError("duplicate candidate symbol")
    if len(candidates) < 25:
        return {}
    ranking = sorted(candidates, key=lambda row: (-row.score, row.symbol))
    k = len(ranking) // 5
    weight = D("0.5") / k
    result = {row.symbol: -weight for row in ranking[:k]}
    result.update({row.symbol: weight for row in ranking[-k:]})
    return result


class BasketState:
    """Retain target notionals without drift, and each last eligible rebalance tier."""

    def __init__(self) -> None:
        self.holdings: dict[str, Decimal] = {}
        self.tiers: dict[str, Decimal] = {}

    @calculated
    def rebalance(
        self, target: dict[str, Decimal], eligible_tiers: dict[str, Decimal]
    ) -> tuple[Decimal, Decimal]:
        tiers = {**self.tiers, **eligible_tiers}
        symbols = sorted(set(target) | set(self.holdings))
        if any(symbol not in tiers for symbol in symbols):
            raise ValueError("missing current or last eligible slippage tier")
        turnover = D(0)
        cost = D(0)
        for symbol in symbols:
            delta = abs(target.get(symbol, D(0)) - self.holdings.get(symbol, D(0)))
            turnover += delta
            cost += delta * (FEE + tiers[symbol])
        self.tiers = tiers
        self.holdings = {symbol: value for symbol, value in target.items() if value}
        return cost, turnover

    @calculated
    def delist(self, symbol: str) -> Decimal:
        """Charge the delisting fee once; the 2% price adjustment belongs to period PnL."""
        return abs(self.holdings.pop(symbol, D(0))) * FEE

    @calculated
    def liquidate(self) -> Decimal:
        """Use only surviving holdings and the tiers retained at t_last, never at e."""
        cost = sum(
            (
                abs(value) * (FEE + self.tiers[symbol])
                for symbol, value in sorted(self.holdings.items())
            ),
            D(0),
        )
        self.holdings = {}
        return cost
