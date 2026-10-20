"""Validated Decimal values. Arithmetic is explicit through the value field."""

from dataclasses import dataclass
from decimal import Decimal

from .enums import Side


def finite_decimal(value: Decimal, name: str) -> Decimal:
    """Reject implicit numeric conversion, NaN and infinity."""
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True, slots=True)
class Qty:
    """Signed quantity: positive long, negative short; zero is a flat position."""

    value: Decimal

    def __post_init__(self) -> None:
        finite_decimal(self.value, "qty")


@dataclass(frozen=True, slots=True)
class Price:
    """Strictly positive finite price."""

    value: Decimal

    def __post_init__(self) -> None:
        if finite_decimal(self.value, "price") <= 0:
            raise ValueError("price must be positive")


def side_and_abs(qty: Qty) -> tuple[Side, Decimal]:
    """Convert a nonzero signed quantity at an exchange adapter boundary."""
    if not isinstance(qty, Qty):
        raise TypeError("qty must be Qty")
    if qty.value == 0:
        raise ValueError("zero has no order side")
    return (Side.BUY if qty.value > 0 else Side.SELL, qty.value.copy_abs())


def signed(side: Side, abs_qty: Decimal) -> Qty:
    """Convert a positive absolute exchange quantity to a signed domain value."""
    if not isinstance(side, Side):
        raise TypeError("side must be Side")
    if finite_decimal(abs_qty, "abs_qty") <= 0:
        raise ValueError("abs_qty must be positive")
    return Qty(abs_qty if side is Side.BUY else abs_qty.copy_negate())
