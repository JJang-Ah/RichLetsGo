"""Validated in-memory inputs for M2-11 synthetic verification (UTC milliseconds)."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal as D

BAR = 300_000
DAY = 86_400_000
B = 1788220800000
CALENDAR = {s: "XNYS" for s in ("MUUSDT", "NVDAUSDT", "SNDKUSDT", "SPCXUSDT")}
CALENDAR["SKHYNIXUSDT"] = "XKRX"


def fetch(*args: object, **kwargs: object) -> None:
    """No registered transport exists at this milestone. Fail before any I/O."""
    raise PermissionError("D104 acquisition is not registered")


def require_symbol(symbol: str) -> None:
    if symbol not in CALENDAR:
        raise ValueError("outside D104 universe")


@dataclass(frozen=True)
class Session:
    symbol: str
    calendar: str
    start: int
    end: int

    def __post_init__(self) -> None:
        require_symbol(self.symbol)
        if (
            self.calendar != CALENDAR[self.symbol]
            or self.start % BAR
            or self.end % BAR
            or self.end - self.start < 12 * BAR
            or not 0 <= self.start < self.end <= B
        ):
            raise ValueError("invalid regular session")


def turnover(at: int, launch: int, days: Mapping[int, D]) -> D | None:
    midnight = at // DAY * DAY
    first = midnight - 30 * DAY
    if not 0 <= at < B:
        raise ValueError("outside design range")
    if first < launch:
        return None
    total = D(0)
    for day in range(first, midnight, DAY):
        value = days.get(day)
        if value is None or not value.is_finite() or value < 0:
            raise ValueError("missing or invalid daily turnover")
        total += value
    return total / 30


def slip(value: D | None) -> D | None:
    if value is None or value < 20_000_000:
        return None
    if value >= 200_000_000:
        return D(".0005")
    return D(".001") if value >= 50_000_000 else D(".0015")


def number(value: D) -> str:
    if not value.is_finite():
        raise ValueError("nonfinite decimal")
    if not value:
        return "0"
    s = format(value, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


@dataclass(frozen=True)
class Candle:
    symbol: str
    at: int
    op: D
    high: D
    low: D
    close: D

    def __post_init__(self) -> None:
        require_symbol(self.symbol)
        if (
            not 0 <= self.at <= B - BAR
            or self.at % BAR
            or not all(v.is_finite() for v in (self.op, self.high, self.low, self.close))
            or not 0 < self.low <= min(self.op, self.close) <= max(self.op, self.close) <= self.high
        ):
            raise ValueError("invalid design candle")


def series(session: Session, rows: Iterable[Candle]) -> tuple[Candle, ...]:
    by: dict[int, Candle] = {}
    for row in rows:
        if (
            row.symbol != session.symbol
            or not session.start <= row.at < session.end
            or by.setdefault(row.at, row) != row
        ):
            raise ValueError("conflicting or unexpected candle")
    if len(by) != (session.end - session.start) // BAR:
        raise ValueError("missing candle")
    return tuple(by[t] for t in sorted(by))


def digest(rows: Iterable[Candle]) -> str:
    """Trade 5m fixture digest, not a campaign manifest."""
    by: dict[tuple[str, int], Candle] = {}
    for r in rows:
        if by.setdefault((r.symbol, r.at), r) != r:
            raise ValueError("conflicting duplicate")
    h = hashlib.sha256()
    for _, r in sorted(by.items()):
        fields = ["trade", r.symbol, "5", r.at, *map(number, (r.op, r.high, r.low, r.close))]
        h.update((json.dumps(fields, separators=(",", ":")) + "\n").encode())
    return h.hexdigest()


def capacity(
    sessions: Iterable[Session], launches: Mapping[str, int], daily: Mapping[str, Mapping[int, D]]
) -> dict[str, int]:
    """Count eligible regular sessions, without accepting any price or signal."""
    counts = dict.fromkeys(CALENDAR, 0)
    end_by_symbol: dict[str, int] = {}
    for session in sorted(set(sessions), key=lambda s: (s.symbol, s.start)):
        sym = session.symbol
        if session.start < end_by_symbol.get(sym, 0):
            raise ValueError("overlapping regular rows")
        end_by_symbol[sym] = session.end
        # Approved XNYS/XKRX regular rows are contained in one UTC day.
        if session.start // DAY != (session.end - 1) // DAY:
            raise ValueError("cross-date session")
        if slip(turnover(session.start, launches[sym], daily[sym])) is not None:
            counts[sym] += 1
    return counts
