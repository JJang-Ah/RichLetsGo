"""Replay fully supplied synthetic sessions. No loaders, sockets or registration."""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal as D

from .execution import FEE, Position, Signal, Terms, breakout, reserve, step
from .inputs import BAR, Candle, Session, number, series, slip, turnover
from .verdict import Trade


@dataclass(frozen=True)
class FundingPeriod:
    """Historical validity [start, end), cadence and a known settlement on that cadence."""

    start: int
    end: int
    interval: int
    anchor: int


def expected_settlements(session: Session, history: tuple[FundingPeriod, ...]) -> set[int]:
    cursor = session.start
    expected: set[int] = set()
    for period in sorted(history, key=lambda p: p.start):
        if (
            period.start >= period.end
            or period.interval <= 0
            or period.interval % BAR
            or period.anchor % BAR
        ):
            raise ValueError("invalid settlement history")
        start, end = max(session.start, period.start), min(session.end, period.end)
        if start >= end:
            continue
        if start != cursor:
            raise ValueError("incomplete or overlapping settlement history")
        first = start + (period.anchor - start) % period.interval
        expected.update(range(first, end, period.interval))
        cursor = end
    if cursor != session.end:
        raise ValueError("incomplete settlement history")
    return expected


@dataclass(frozen=True)
class Fixture:
    session: Session
    trade: tuple[Candle, ...]
    mark: tuple[Candle, ...]
    launch: int
    daily: Mapping[int, D]
    terms: Terms
    # Explicit scheduled settlements; missing required mark/rate is invalid.
    settlements: Mapping[int, tuple[D, D]]
    funding_history: tuple[FundingPeriod, ...]


@dataclass(frozen=True)
class Replay:
    equity: D
    mdd: D
    minimum_equity: D
    entries: tuple[tuple[int, str], ...]
    trades: tuple[Trade, ...]
    cost_rejections: Mapping[tuple[str, D], int]
    contention: Mapping[str, int]
    costs: tuple[tuple[str, D, D, D, D], ...]
    mdd_at: int | None
    digest: str


def replay(fixtures: Iterable[Fixture]) -> Replay:
    bars: dict[int, dict[str, tuple[Fixture, Candle, Candle]]] = defaultdict(dict)
    signals: dict[int, list[tuple[Signal, Terms]]] = defaultdict(list)
    for f in fixtures:
        s = f.session
        trade = series(s, f.trade)
        marks = series(s, f.mark)
        if expected_settlements(s, f.funding_history) != f.settlements.keys() or any(
            v is None for v in f.settlements.values()
        ):
            raise ValueError("unexpected settlement boundary")
        # Both admitted calendars fit within one UTC date (including early closes).
        if s.start // 86_400_000 != (s.end - 1) // 86_400_000:
            raise ValueError("cross-date session")
        tier = slip(turnover(s.start, f.launch, f.daily))
        for b, m in zip(trade, marks, strict=True):
            if s.symbol in bars[b.at]:
                raise ValueError("overlapping sessions")
            bars[b.at][s.symbol] = (f, b, m)
        if tier is not None:
            signal = breakout(s, trade)
            if signal:
                signals[signal.at].append((signal, replace(f.terms, slip=tier)))
    cash = equity = peak = minimum_equity = D(10000)
    mdd = D(0)
    holdings: dict[str, Position] = {}
    last: dict[str, D] = {}
    entries: list[tuple[int, str]] = []
    trades: list[Trade] = []
    episode: dict[str, D] = {}
    cost_rejections: Counter[tuple[str, D]] = Counter()
    contention: Counter[str] = Counter()
    costs: list[tuple[str, D, D, D, D]] = []
    entry_cost: dict[str, tuple[D, D, D]] = {}
    mdd_at = None
    transcript = hashlib.sha256()
    for at, group in sorted(bars.items()):
        if holdings.keys() - group.keys():
            raise ValueError("unmarked open position")

        def notional() -> dict[str, D]:
            return {sym: pos.reservation.qty * last[sym] for sym, pos in holdings.items()}

        pending = {
            r.signal.symbol: r
            for r in reserve(
                signals[at],
                equity,
                notional(),
                cost_rejections=cost_rejections,
                contention=contention,
            )
        }
        # Pending quantities were fixed at the previous close. An intrabar exit
        # must not create capacity for another order at this bar's open.
        used = notional()
        for sym, existing in holdings.items():
            f, bar, _ = group[sym]
            sig = existing.reservation.signal
            if at == f.session.end - BAR or (bar.op - sig.stop) * sig.side <= 0:
                del used[sym]
        stress = cash
        for sym in sorted(group):
            f, b, m = group[sym]
            pos = holdings.get(sym)
            settlement = f.settlements.get(at)
            r = pending.get(sym)
            free = equity * D(".75") - sum(used.values())
            if r is not None:
                opening_notional = r.qty * r.terms.price(b.op, r.signal.side)
                if (
                    (b.op - r.signal.stop) * r.signal.side > 0
                    and r.terms.min_value <= opening_notional <= r.cap
                    and opening_notional > free
                ):
                    contention[sym] += 1
            result = step(b, f.session, pos, r, settlement, free)
            opened = r is not None and (
                result.position is not None or result.exit_price is not None
            )
            if opened and r:
                entries.append((at, sym))
                pos = Position(r, r.terms.price(b.op, r.signal.side))
                used[sym] = pos.entry * r.qty
                entry_cost[sym] = (abs(pos.entry - b.op) * r.qty, pos.entry * r.qty * FEE, D(0))
            if pos:
                pr = pos.reservation
                adverse = m.low if pr.signal.side == 1 else m.high
                stress += pr.qty * (adverse - pos.entry) * pr.signal.side
                if opened:
                    stress -= pr.qty * pos.entry * FEE
                elif settlement:
                    stress -= pr.qty * pr.signal.side * settlement[0] * settlement[1]
                if result.exit_price is not None:
                    stress -= pr.qty * result.exit_price * FEE
            if pos:
                paid_slip, fees, funding_cash = entry_cost[sym]
                if settlement and not opened:
                    funding_cash -= (
                        pos.reservation.qty
                        * pos.reservation.signal.side
                        * settlement[0]
                        * settlement[1]
                    )
                entry_cost[sym] = paid_slip, fees, funding_cash
                episode[sym] = episode.get(sym, D(0)) + result.cash_change
                if result.exit_price is not None:
                    qty = pos.reservation.qty
                    sig = pos.reservation.signal
                    base = (
                        b.op
                        if at == f.session.end - BAR or (b.op - sig.stop) * sig.side <= 0
                        else sig.stop
                    )
                    costs.append(
                        (
                            sym,
                            paid_slip + qty * abs(result.exit_price - base),
                            fees + qty * FEE * result.exit_price,
                            funding_cash,
                            qty * abs(sig.close - sig.stop),
                        )
                    )
                    del entry_cost[sym]
                    trades.append(
                        Trade(
                            datetime.fromtimestamp(at // 1000, UTC).date(),
                            at,
                            sym,
                            episode.pop(sym),
                            pos.reservation.risk,
                        )
                    )
            cash += result.cash_change
            if result.position:
                holdings[sym] = result.position
            else:
                holdings.pop(sym, None)
            last[sym] = m.close
            line = [
                at,
                sym,
                opened,
                number(result.cash_change),
                number(result.exit_price) if result.exit_price is not None else None,
            ]
            transcript.update((json.dumps(line, separators=(",", ":")) + "\n").encode())
        equity = cash + sum(
            p.reservation.qty * (last[k] - p.entry) * p.reservation.signal.side
            for k, p in holdings.items()
        )
        drawdown = (peak - min(stress, equity)) / peak
        if drawdown > mdd:
            mdd, mdd_at = drawdown, at
        minimum_equity = min(minimum_equity, stress, equity)
        peak = max(peak, equity)
        transcript.update((number(equity) + " " + number(mdd) + "\n").encode())
    if holdings:
        raise ValueError("non-flat terminal account")
    return Replay(
        equity,
        mdd,
        minimum_equity,
        tuple(entries),
        tuple(trades),
        dict(sorted(cost_rejections.items())),
        dict(sorted(contention.items())),
        tuple(costs),
        mdd_at,
        transcript.hexdigest(),
    )
