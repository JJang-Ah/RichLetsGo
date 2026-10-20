"""Injected exchange increments; no instrument lookup or risk defaults."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from .enums import Symbol
from .values import finite_decimal


def _round_step(value: Decimal, step: Decimal, *, up: bool) -> Decimal:
    # Include coefficient and exponent differences so caller precision cannot
    # round a quotient across a step boundary before floor/ceiling is applied.
    with localcontext() as ctx:
        ctx.prec = max(
            28,
            len(value.as_tuple().digits)
            + len(step.as_tuple().digits)
            + abs(value.adjusted() - step.adjusted())
            + 4,
        )
        units = (value / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
        return units * step


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    symbol: Symbol
    qty_step: Decimal
    min_qty: Decimal
    tick_size: Decimal
    max_leverage: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, Symbol):
            raise TypeError("symbol must be Symbol")
        for name in ("qty_step", "min_qty", "tick_size", "max_leverage"):
            if finite_decimal(getattr(self, name), name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_leverage < 1:
            raise ValueError("max_leverage must be at least one")

    def round_qty_down(self, abs_qty: Decimal) -> Decimal:
        """Floor to an increment; never raise a small order to the minimum."""
        if finite_decimal(abs_qty, "abs_qty") < 0:
            raise ValueError("abs_qty must be nonnegative")
        result = _round_step(abs_qty, self.qty_step, up=False)
        return Decimal(0) if result < self.min_qty else result

    def round_price_down(self, price: Decimal) -> Decimal:
        """Floor to a tick; zero is possible below one tick and is not a Price."""
        if finite_decimal(price, "price") <= 0:
            raise ValueError("price must be positive")
        return _round_step(price, self.tick_size, up=False)

    def round_price_up(self, price: Decimal) -> Decimal:
        """Ceil to a tick. Stop-direction selection belongs to M1-03."""
        if finite_decimal(price, "price") <= 0:
            raise ValueError("price must be positive")
        return _round_step(price, self.tick_size, up=True)
