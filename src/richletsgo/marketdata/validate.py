"""Parse and validate raw Bybit rows. Never invent bars or funding records."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from richletsgo.domain import Symbol
from richletsgo.domain.clock import require_utc

from .models import Bar, FundingRate, Interval, from_ms

type RawKline = Sequence[str]


@dataclass(frozen=True, slots=True)
class RejectedRow:
    reason: str
    raw: tuple[str, ...]


@dataclass(slots=True)
class BarBatch:
    bars: list[Bar] = field(default_factory=list)
    rejected: list[RejectedRow] = field(default_factory=list)
    conflicts: list[datetime] = field(default_factory=list)
    in_progress_dropped: int = 0


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a decimal: {text!r}") from exc


def parse_klines(
    rows: Iterable[RawKline], symbol: Symbol, interval: Interval, collected_at: datetime
) -> BarBatch:
    """Keep only closed bars (start + interval <= collected_at), sorted ascending.

    Bybit returns the in-progress candle too (its close is the last trade), so it is
    dropped here; this is where look-ahead prevention starts (04 3.1).
    """
    require_utc(collected_at)
    batch = BarBatch()
    by_start: dict[datetime, Bar] = {}
    for row in rows:
        raw = tuple(str(v) for v in row)
        try:
            if len(raw) != 7:
                raise ValueError("expected 7 fields")
            start = from_ms(int(raw[0]))
            if start + interval.duration > collected_at:
                batch.in_progress_dropped += 1
                continue
            bar = Bar(symbol, interval, start, *(_decimal(v) for v in raw[1:]))
        except (ValueError, TypeError) as exc:
            batch.rejected.append(RejectedRow(str(exc), raw))
            continue
        previous = by_start.get(bar.start)
        if previous is None:
            by_start[bar.start] = bar
        elif not previous.same_values(bar):
            batch.conflicts.append(bar.start)
    conflicted = set(batch.conflicts)
    batch.bars = [by_start[k] for k in sorted(by_start) if k not in conflicted]
    return batch


def parse_funding(rows: Iterable[dict[str, str]], symbol: Symbol) -> list[FundingRate]:
    out: dict[datetime, FundingRate] = {}
    for row in rows:
        if row.get("symbol") != symbol.value:
            continue
        rate = FundingRate(
            symbol, from_ms(int(row["fundingRateTimestamp"])), _decimal(row["fundingRate"])
        )
        existing = out.get(rate.funding_time)
        if existing is not None and existing.rate != rate.rate:
            raise ValueError(f"conflicting funding rates at {rate.funding_time.isoformat()}")
        out[rate.funding_time] = rate
    return [out[k] for k in sorted(out)]


def expected_starts(interval: Interval, first: datetime, last: datetime) -> list[datetime]:
    """Aligned bar starts in [first, last]; both ends are floored/ceiled to the interval."""
    start = interval.floor(first)
    if start < first:
        start += interval.duration
    result = []
    cursor = start
    while cursor <= last:
        result.append(cursor)
        cursor += interval.duration
    return result


def contiguous_ranges(
    starts: Sequence[datetime], step: timedelta
) -> list[tuple[datetime, datetime]]:
    """Group sorted starts into inclusive [first, last] runs."""
    ranges: list[tuple[datetime, datetime]] = []
    for ts in starts:
        if ranges and ts - ranges[-1][1] == step:
            ranges[-1] = (ranges[-1][0], ts)
        else:
            ranges.append((ts, ts))
    return ranges


def missing_bar_ranges(
    interval: Interval, have: Iterable[datetime], first: datetime, last: datetime
) -> list[tuple[datetime, datetime]]:
    present = set(have)
    missing = [ts for ts in expected_starts(interval, first, last) if ts not in present]
    return contiguous_ranges(missing, interval.duration)


def funding_gaps(
    times: Sequence[datetime], interval: timedelta, first: datetime, last: datetime
) -> list[tuple[datetime, datetime]]:
    """D87: a gap is any stretch longer than the captured funding interval between
    neighbouring records, or between the range edges and the nearest record."""
    points = [first, *sorted(t for t in times if first <= t <= last), last]
    return [(a, b) for a, b in zip(points, points[1:], strict=False) if b - a > interval]
