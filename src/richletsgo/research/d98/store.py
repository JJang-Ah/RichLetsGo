"""SQLite series with exact decimal text, immutable rows and durable page checkpoints."""

import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from itertools import batched
from pathlib import Path
from typing import Any

from richletsgo.backtest.codec import encode


class Store:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = path
        if readonly:
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        for table, columns in [
            (
                "bars_1h",
                "symbol TEXT, start INTEGER, open TEXT, high TEXT, low TEXT, close TEXT, volume TEXT, turnover TEXT, PRIMARY KEY(symbol,start)",
            ),
            (
                "btc_daily",
                "symbol TEXT, start INTEGER, open TEXT, high TEXT, low TEXT, close TEXT, volume TEXT, turnover TEXT, PRIMARY KEY(symbol,start)",
            ),
            ("funding", "symbol TEXT, time INTEGER, rate TEXT, PRIMARY KEY(symbol,time)"),
        ]:
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS collection_log (id INTEGER PRIMARY KEY, content TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS identity (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def bind(self, pool_digest: str) -> None:
        row = self.db.execute("SELECT value FROM identity WHERE key='pool'").fetchone()
        if row is not None and row[0] != pool_digest:
            raise ValueError("database belongs to another pool")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO identity VALUES ('pool', ?)", (pool_digest,))

    def ensure_mutable(self) -> None:
        if self.db.execute("SELECT 1 FROM identity WHERE key='frozen'").fetchone():
            raise ValueError("registered dataset is frozen")

    def freeze(self, registration_digest: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO identity VALUES ('frozen', ?)", (registration_digest,))

    @staticmethod
    def table(kind: str) -> str:
        return {"bars": "bars_1h", "daily": "btc_daily", "funding": "funding"}[kind]

    def put(self, kind: str, symbol: str, rows: Iterable[list[Any]]) -> None:
        self.ensure_mutable()
        table = self.table(kind)
        column = "time" if kind == "funding" else "start"
        for batch in batched(rows, 1000):
            normalized: dict[int, tuple[Any, ...]] = {}
            for row in batch:
                stamp = row[0]
                if (
                    isinstance(stamp, bool)
                    or not isinstance(stamp, (int, str))
                    or (isinstance(stamp, str) and not stamp.isdigit())
                ):
                    raise ValueError("timestamp must be integer milliseconds")
                if any(isinstance(v, (float, bool)) for v in row[1:]):
                    raise ValueError("prices/rates require decimal strings or Decimal")
                time = int(stamp)
                values = [Decimal(str(v)) for v in row[1:]]
                if len(values) != (1 if kind == "funding" else 6) or not all(
                    v.is_finite() for v in values
                ):
                    raise ValueError("invalid numeric row")
                texts = [format(v, "f") for v in values]
                texts = [v.rstrip("0").rstrip(".") if "." in v else v for v in texts]
                item = (symbol, time, *texts)
                if time in normalized and normalized[time] != item:
                    raise ValueError("conflicting duplicate row")
                normalized[time] = item
            existing = self.db.execute(
                f"SELECT * FROM {table} WHERE symbol=? AND {column} BETWEEN ? AND ?",
                (symbol, min(normalized), max(normalized)),
            )
            for old in existing:
                if old[1] in normalized and old != normalized[old[1]]:
                    raise ValueError(f"conflicting {kind} row: {symbol} {old[1]}")
            placeholders = ",".join("?" for _ in next(iter(normalized.values())))
            self.db.executemany(
                f"INSERT OR IGNORE INTO {table} VALUES ({placeholders})", normalized.values()
            )

    def rows(self, kind: str, symbol: str) -> Iterator[list[Any]]:
        table = self.table(kind)
        for row in self.db.execute(
            f"SELECT * FROM {table} WHERE symbol=? ORDER BY {'time' if kind == 'funding' else 'start'}",
            (symbol,),
        ):
            yield [row[1], *row[2:]]

    def times(self, kind: str, symbol: str) -> list[int]:
        column = "time" if kind == "funding" else "start"
        return [
            r[0]
            for r in self.db.execute(
                f"SELECT {column} FROM {self.table(kind)} WHERE symbol=? ORDER BY {column}",
                (symbol,),
            )
        ]

    def log(self, value: Any) -> None:
        with self.db:
            self.db.execute("INSERT INTO collection_log(content) VALUES (?)", (encode(value),))


@contextmanager
def dataset_lock(path: Path) -> Iterator[None]:
    """Single collector/auditor; never remove another process's lock or stale lock silently."""
    lock = path.with_suffix(path.suffix + ".lock")
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    try:
        yield
    finally:
        lock.unlink()
