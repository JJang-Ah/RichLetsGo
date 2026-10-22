"""Decimal-only indicators, evaluated at the last bar of a validated prefix.

A fixed 34-digit context keeps results independent of the caller's Decimal
context. Wilder seeds use the entire supplied prefix; callers must retain the
same history origin across evaluations (or implement equivalent persisted seeds).
"""

from dataclasses import dataclass
from decimal import Context, Decimal, localcontext

from richletsgo.domain.values import finite_decimal

from .history import ClosedBars

CALC = Context(prec=34)
ZERO = Decimal(0)


def period(n: int) -> None:
    if type(n) is not int or n < 1:
        raise ValueError("period must be a positive integer")


def positive(value: Decimal, name: str) -> None:
    if finite_decimal(value, name) <= 0:
        raise ValueError(f"{name} must be positive")


def sma(history: ClosedBars, n: int) -> Decimal | None:
    """sum(last n closes) / n; warm-up n closed bars."""
    period(n)
    if len(history.bars) < n:
        return None
    with localcontext(CALC):
        return sum((b.close for b in history.bars[-n:]), ZERO) / n


@dataclass(frozen=True, slots=True)
class Channel:
    high: Decimal
    low: Decimal


def channel(history: ClosedBars, n: int, *, include_current: bool) -> Channel | None:
    """max(high), min(low) over n bars; warm-up n (included) or n+1 (excluded)."""
    period(n)
    if type(include_current) is not bool:
        raise TypeError("include_current must be explicit bool")
    bars = history.bars if include_current else history.bars[:-1]
    if len(bars) < n:
        return None
    return Channel(max(b.high for b in bars[-n:]), min(b.low for b in bars[-n:]))


@dataclass(frozen=True, slots=True)
class Bands:
    mid: Decimal
    upper: Decimal
    lower: Decimal


def bollinger(history: ClosedBars, n: int, k: Decimal) -> Bands | None:
    """SMA +/- k*sqrt(sum((close-SMA)^2)/n), population sigma; warm-up n."""
    period(n)
    positive(k, "band multiplier")
    mid = sma(history, n)
    if mid is None:
        return None
    with localcontext(CALC):
        variance = sum(((b.close - mid) ** 2 for b in history.bars[-n:]), ZERO) / n
        width = k * variance.sqrt()
        return Bands(mid, mid + width, mid - width)


def rsi(history: ClosedBars, n: int) -> Decimal | None:
    """Wilder RSI; seed mean of n close changes (n+1 bars), then (old*(n-1)+x)/n.

    RSI=100*gain/(gain+loss). All-flat=50, gain-only=100, loss-only=0.
    """
    period(n)
    if len(history.bars) < n + 1:
        return None
    with localcontext(CALC):
        changes = [b.close - a.close for a, b in zip(history.bars, history.bars[1:], strict=False)]
        gains = [max(ZERO, x) for x in changes]
        losses = [max(ZERO, -x) for x in changes]
        gain, loss = sum(gains[:n], ZERO) / n, sum(losses[:n], ZERO) / n
        for g, loss_value in zip(gains[n:], losses[n:], strict=True):
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + loss_value) / n
        return Decimal(50) if gain + loss == 0 else 100 * gain / (gain + loss)


def atr(history: ClosedBars, n: int) -> Decimal | None:
    """TR=max(high-low, abs(high-prev_close), abs(low-prev_close)).

    First bar TR=high-low. Seed mean of first n TRs (warm-up n bars),
    then Wilder smoothing: (previous_ATR*(n-1)+TR)/n.
    """
    period(n)
    if len(history.bars) < n:
        return None
    with localcontext(CALC):
        ranges = []
        previous = None
        for bar in history.bars:
            tr = bar.high - bar.low
            if previous is not None:
                tr = max(tr, abs(bar.high - previous.close), abs(bar.low - previous.close))
            ranges.append(tr)
            previous = bar
        value = sum(ranges[:n], ZERO) / n
        for tr in ranges[n:]:
            value = (value * (n - 1) + tr) / n
        return value
