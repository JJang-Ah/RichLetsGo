"""Offline breakout, reservation and bar-event contracts for synthetic tests."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D

from .inputs import BAR, Candle, Session, require_symbol, series

FEE = D(".00055")


@dataclass(frozen=True)
class Signal:
    symbol: str
    at: int
    side: int
    close: D
    stop: D


def breakout(session: Session, rows: Iterable[Candle]) -> Signal | None:
    bars = series(session, rows)
    hi = max(b.high for b in bars[:6])
    lo = min(b.low for b in bars[:6])
    if hi == lo:
        return None
    for b in bars[6:]:
        at = b.at + BAR
        if at >= session.end - 2 * BAR:
            break
        if b.close > hi or b.close < lo:
            side = 1 if b.close > hi else -1
            return Signal(session.symbol, at, side, b.close, lo if side == 1 else hi)
    return None


@dataclass(frozen=True)
class Terms:
    slip: D
    tick: D
    step: D
    min_qty: D
    min_value: D

    def __post_init__(self) -> None:
        if (
            self.slip not in {D(".0015"), D(".001"), D(".0005")}
            or min(self.tick, self.step) <= 0
            or min(self.min_qty, self.min_value) < 0
        ):
            raise ValueError("invalid terms")

    def price(self, base: D, side: int) -> D:
        units = base * (1 + side * self.slip) / self.tick
        return (
            units.to_integral_value(rounding=ROUND_CEILING if side == 1 else ROUND_FLOOR)
            * self.tick
        )


@dataclass(frozen=True)
class Reservation:
    signal: Signal
    terms: Terms
    qty: D
    risk: D
    cap: D


def reserve(
    signals: Iterable[tuple[Signal, Terms]], equity: D, held: Mapping[str, D]
) -> list[Reservation]:
    """One signal boundary; held values are absolute previous-mark notionals."""
    left = equity * D(".75") - sum(held.values())
    result = []
    seen = set(held)
    batch = sorted(signals, key=lambda pair: pair[0].symbol)
    if len({s.at for s, _ in batch}) > 1:
        raise ValueError("mixed boundaries")
    for s, terms in batch:
        require_symbol(s.symbol)
        if s.symbol in seen or len(seen) >= 3 or left <= 0:
            continue
        price = terms.price(s.close, s.side)
        stop = terms.price(s.stop, -s.side)
        unit = abs(price - stop) + FEE * (price + stop)
        cap = min(equity * D(".25"), left)
        q = (min(equity * D(".0025") / unit, cap / price) // terms.step) * terms.step
        if q <= 0 or q < terms.min_qty or q * price < terms.min_value:
            continue
        result.append(Reservation(s, terms, q, unit * q, cap))
        left -= q * price
        seen.add(s.symbol)
    return result


@dataclass(frozen=True)
class Position:
    reservation: Reservation
    entry: D


@dataclass(frozen=True)
class Step:
    position: Position | None
    cash_change: D
    exit_price: D | None


def step(
    bar: Candle,
    session: Session,
    position: Position | None,
    order: Reservation | None = None,
    funding: tuple[D, D] | None = None,
    free_notional: D | None = None,
) -> Step:
    """One symbol, one bar: settlement, open exit, reserved entry, intrabar stop.
    funding is (signed rate, settlement mark) at this boundary, if scheduled.
    """
    if bar.symbol != session.symbol or not session.start <= bar.at < session.end:
        raise ValueError("bar outside session")
    cash = D(0)

    def close(pos: Position, base: D) -> Step:
        r = pos.reservation
        price = r.terms.price(base, -r.signal.side)
        return Step(None, cash + r.qty * ((price - pos.entry) * r.signal.side - price * FEE), price)

    if position:
        r = position.reservation
        if r.signal.symbol != bar.symbol:
            raise ValueError("position mismatch")
        if funding:
            cash -= r.signal.side * r.qty * funding[0] * funding[1]
        if (bar.op - r.signal.stop) * r.signal.side <= 0 or bar.at == session.end - BAR:
            return close(position, bar.op)
    if order:
        s = order.signal
        if position or s.symbol != bar.symbol or s.at != bar.at:
            raise ValueError("reservation mismatch")
        price = order.terms.price(bar.op, s.side)
        cap = order.cap if free_notional is None else min(order.cap, free_notional)
        if (
            (bar.op - s.stop) * s.side <= 0
            or order.qty * price > cap
            or order.qty * price < order.terms.min_value
        ):
            return Step(None, cash, None)
        position = Position(order, price)
        cash -= order.qty * price * FEE
    if position:
        s = position.reservation.signal
        if bar.low <= s.stop if s.side == 1 else bar.high >= s.stop:
            return close(position, s.stop)
    return Step(position, cash, None)
