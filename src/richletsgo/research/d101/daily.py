"""UTC daily bars built only from complete hourly groups."""

from collections import defaultdict

from richletsgo.research.d98.boundaries import DAY, HOUR
from richletsgo.research.d98.models import D, RawBar, calculated


@calculated
def daily_bars(hourly: list[RawBar]) -> dict[int, RawBar]:
    """Aggregate [d,d+24h) when every UTC-aligned hourly bar is present."""
    by_time = {bar.start: bar for bar in hourly}
    if len(by_time) != len(hourly):
        raise ValueError("duplicate hourly bar")
    groups: dict[int, list[RawBar]] = defaultdict(list)
    for bar in hourly:
        if bar.start % HOUR:
            raise ValueError("hourly bar is off UTC grid")
        groups[bar.start // DAY * DAY].append(bar)
    result: dict[int, RawBar] = {}
    for start, rows in groups.items():
        rows.sort(key=lambda row: row.start)
        if len(rows) != 24 or any(row.start != start + i * HOUR for i, row in enumerate(rows)):
            continue
        first, last = rows[0], rows[-1]
        result[start] = RawBar(
            start,
            first.open,
            max(row.high for row in rows),
            min(row.low for row in rows),
            last.close,
            sum((row.volume for row in rows), D(0)),
            sum((row.turnover for row in rows), D(0)),
        )
    return result
