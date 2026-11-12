"""Display formatting: KST times and Decimal values without float conversion."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

KST = timezone(timedelta(hours=9), "KST")


def kst(at: datetime | None, pattern: str = "%Y-%m-%d %H:%M") -> str:
    if at is None:
        return "-"
    if at.tzinfo is None:
        raise ValueError("naive datetime")
    return at.astimezone(KST).strftime(pattern)


def num(value: Decimal | None) -> str:
    """Decimal as stored, with thousands separators; never through float."""
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    text = format(abs(value), "f")
    whole, _, frac = text.partition(".")
    grouped = f"{int(whole):,}"
    return f"{sign}{grouped}.{frac}" if frac else f"{sign}{grouped}"


def money(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return num(value.quantize(Decimal("0.01")))


def signed(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return ("+" if value > 0 else "") + money(value)


def pct(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "-"
    return num((value * 100).quantize(Decimal(1).scaleb(-places))) + "%"
