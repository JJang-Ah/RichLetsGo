"""Generate the committed D103 session table from exchange calendars (run once, offline).

Requires the optional `exchange_calendars` package (not a project dependency). The output
records the library version so labels are reproducible without a timezone database.
"""

import argparse
import importlib.metadata
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xc  # type: ignore[import-untyped]

from richletsgo.backtest.codec import encode
from richletsgo.research.d103.sessions import regular_intervals, us_extended

START, END = "2026-03-01", "2026-12-31"
NEW_YORK = ZoneInfo("America/New_York")


def ms(value: object) -> int | None:
    if value is None or str(value) == "NaT":
        return None
    return int(value.timestamp() * 1000)  # type: ignore[attr-defined]


def wall(day: date, hour: int, minute: int) -> int:
    return int(datetime.combine(day, time(hour, minute), NEW_YORK).timestamp() * 1000)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    calendars = {}
    for name in ("XNYS", "XKRX", "XHKG", "XSHG", "CMES"):
        schedule = xc.get_calendar(name, start=START, end=END).schedule
        rows = [
            [ms(r.open), ms(r.break_start), ms(r.break_end), ms(r.close)]
            for r in schedule.itertuples()
        ]
        if name == "XNYS":
            days = [
                (datetime.fromtimestamp(o / 1000, UTC).astimezone(NEW_YORK).date(), o, c)
                for o, _, _, c in rows
                if o is not None and c is not None
            ]
            calendars[name] = [list(r) for r in us_extended(days, wall)]
        else:
            calendars[name] = [list(r) for r in regular_intervals(rows)]
    first = int(datetime.fromisoformat(START).replace(tzinfo=UTC).timestamp() * 1000)
    last = int(datetime.fromisoformat(END).replace(tzinfo=UTC).timestamp() * 1000)
    table = {
        "source": "exchange_calendars",
        "version": importlib.metadata.version("exchange_calendars"),
        "range": [first, last],
        "notes": "XNYS adds overnight 20:00 (D-1) / pre 04:00 / post 16:00-20:00 ET. "
        "CMES (commodity, forex) is an approximation of the underlying sessions.",
        "calendars": calendars,
    }
    args.output.write_text(encode(table) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
