"""Session labels from a committed calendar table (M2-10 section 4).

The table is generated once by tools/d103_sessions.py from exchange calendars, so this
module needs no timezone database. Gaps between intervals are "closed"; times outside
the table range are "unknown".
"""

from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .allowlist import Entry
from .models import AFTER, BEFORE, BOUNDARY

LABELS = frozenset({"regular", "break", "pre", "post", "overnight"})


def calendar_for(entry: Entry) -> str:
    if entry.symbol_type in ("commodity", "forex"):
        return "CMES"
    region = {"US": "XNYS", "KR": "XKRX", "HK": "XHKG"}.get(entry.region)
    if region is not None:
        return region
    if entry.region == "CN" and entry.ticker.startswith("6"):
        return "XSHG"
    raise ValueError(f"no calendar for {entry.symbol} ({entry.region} {entry.ticker})")


def regular_intervals(days: Sequence[Sequence[int | None]]) -> list[tuple[int, int, str]]:
    """[open, break_start, break_end, close] rows to regular/break intervals."""
    out: list[tuple[int, int, str]] = []
    for open_, break_start, break_end, close in days:
        assert open_ is not None and close is not None
        if break_start is None or break_end is None:
            out.append((open_, close, "regular"))
        else:
            out += [
                (open_, break_start, "regular"),
                (break_start, break_end, "break"),
                (break_end, close, "regular"),
            ]
    return out


def us_extended(
    days: Sequence[tuple[date, int, int]], wall: Callable[[date, int, int], int]
) -> list[tuple[int, int, str]]:
    """NYSE days (ET date, open, close) plus overnight 20:00 (D-1) to pre 04:00 to 20:00 post.

    `wall(d, hour, minute)` converts New York wall time to UTC ms, so DST is the caller's.
    """
    out: list[tuple[int, int, str]] = []
    for day, open_, close in days:
        overnight = wall(day - timedelta(days=1), 20, 0)
        pre = wall(day, 4, 0)
        post_end = wall(day, 20, 0)
        if not overnight < pre <= open_ < close <= post_end:
            raise ValueError(f"inconsistent US session on {day}")
        out += [
            (overnight, pre, "overnight"),
            (pre, open_, "pre"),
            (open_, close, "regular"),
            (close, post_end, "post"),
        ]
    return out


@dataclass(frozen=True)
class Boundary:
    at: int
    label: str
    previous: str


class Sessions:
    def __init__(self, intervals: Sequence[Sequence[Any]], first: int, last: int) -> None:
        rows = sorted((int(a), int(b), str(c)) for a, b, c in intervals if int(a) < int(b))
        if any(label not in LABELS for _, _, label in rows):
            raise ValueError("unknown session label")
        if any(nxt[0] < cur[1] for cur, nxt in zip(rows, rows[1:], strict=False)):
            raise ValueError("overlapping session intervals")
        self.rows = rows
        self.starts = [r[0] for r in rows]
        self.first, self.last = first, last

    def label(self, at: int) -> str:
        if not self.first <= at < self.last:
            return "unknown"
        index = bisect_right(self.starts, at) - 1
        if index >= 0 and self.rows[index][0] <= at < self.rows[index][1]:
            return self.rows[index][2]
        return "closed"

    def boundaries(self) -> list[Boundary]:
        """Starts of each labelled interval whose preceding moment has a different label."""
        out = []
        for start, _, label in self.rows:
            previous = self.label(start - 1)
            if previous != label and previous != "unknown":
                out.append(Boundary(start, label, previous))
        return out


def is_reopen(b: Boundary) -> bool:
    """M2-10 4 session starts R: pre or regular start, or the first session after a closure.

    Session ends (regular->post, regular->break) and post->overnight are not R.
    """
    return b.label in ("pre", "regular") or b.previous == "closed"


def reopen_points(sessions: Sessions, launch: int) -> list[Boundary]:
    """R whose whole comparison window lies in the observed range [launch, B)."""
    return [
        b
        for b in sessions.boundaries()
        if is_reopen(b) and b.at + BEFORE[0] >= launch and b.at + AFTER[1] <= BOUNDARY
    ]


def load(table: dict[str, Any], name: str) -> Sessions:
    first, last = table["range"]
    return Sessions(table["calendars"][name], int(first), int(last))
