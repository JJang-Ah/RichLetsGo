"""Small D101 datasets built entirely in temporary SQLite files."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from d98_fixtures import contract, daily_bars, make_pool, raw

from richletsgo.research.d98.audit import audit
from richletsgo.research.d98.boundaries import DAY, HOUR, START, STUDY_START
from richletsgo.research.d98.models import D, RawBar
from richletsgo.research.d98.store import Store


def hourly_dataset(
    path: Path,
    *,
    end: int | None = None,
    reverse_on_day_two: bool = False,
    judgment: bool = False,
    freeze: str = "f7388c0737eafecee0ead281c8aea486c3155ba9c32303742a53273b3106d2e3",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create one DESIGN symbol, optional poison JUDGMENT rows and a frozen marker."""
    end = end or STUDY_START + 3 * DAY
    design = contract(end=end)
    contracts = [design]
    if judgment:
        unseen = contract("POISONUSDT", end=end, key="UNSEEN")
        unseen["pool"] = "JUDGMENT"
        contracts.append(unseen)
    pool = make_pool(contracts)
    store = Store(path)
    try:
        store.bind(pool["sha256"])
        signal_day = STUDY_START - DAY

        def hourly_rows() -> Iterator[list[Any]]:
            for day in range(START, end, DAY):
                opening, high, low, close = D(100), D(105), D(95), D(100)
                if day == signal_day:
                    opening, high, low, close = D(100), D(122), D(95), D(120)
                elif day == STUDY_START:
                    opening, high, low, close = (
                        (D(100), D(100), D(90), D(90))
                        if reverse_on_day_two
                        else (D(100), D(105), D(100), D(102))
                    )
                for hour in range(24):
                    at = day + hour * HOUR
                    bar_open = opening if hour == 0 else close
                    yield raw(RawBar(at, bar_open, high, low, close, D(10), D(300_000)))

        with store.db:
            store.put("bars", design["symbol"], hourly_rows())
            store.put(
                "funding",
                design["symbol"],
                ([at, "0.0001"] for at in range(START - DAY, end + 1, 4 * HOUR)),
            )
            store.put("daily", "BTCUSDT", (raw(bar) for bar in daily_bars()))
            if judgment:
                # Malformed numeric payload proves the study never reads JUDGMENT rows.
                store.db.execute(
                    "INSERT INTO bars_1h VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("POISONUSDT", START, "not-decimal", "x", "x", "x", "x", "x"),
                )
        store.freeze(freeze)
    finally:
        store.close()
    return pool, audit(pool, path)


def change_one_bar(path: Path, symbol: str, at: int, close: str) -> None:
    store = Store(path)
    try:
        row = store.db.execute(
            "SELECT open, high, low, volume, turnover FROM bars_1h WHERE symbol=? AND start=?",
            (symbol, at),
        ).fetchone()
        if row is None:
            raise ValueError("fixture bar not found")
        store.db.execute(
            "UPDATE bars_1h SET close=? WHERE symbol=? AND start=?",
            (close, symbol, at),
        )
        store.db.commit()
    finally:
        store.close()


def add_daily(
    rows: dict[int, RawBar], at: int, *, opening: str, high: str, low: str, close: str
) -> None:
    rows[at] = RawBar(at, D(opening), D(high), D(low), D(close), D(0), D(0))
