"""Separate SQLite for D103 structure data. Never opens a D98 dataset (M2-10 3)."""

import hashlib
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from richletsgo.backtest.codec import encode

TABLES = {
    "bars": "kind TEXT, interval TEXT, symbol TEXT, start INTEGER, open TEXT, high TEXT, "
    "low TEXT, close TEXT, volume TEXT, turnover TEXT, PRIMARY KEY(kind,interval,symbol,start)",
    "funding": "symbol TEXT, time INTEGER, rate TEXT, PRIMARY KEY(symbol,time)",
    # at = schedule slot (checkpoint only); requested = request start; ts = book time.
    "book_top": "symbol TEXT, at INTEGER, requested INTEGER, ts INTEGER, bid TEXT, "
    "bid_qty TEXT, ask TEXT, ask_qty TEXT, PRIMARY KEY(symbol,at)",
    "book_depth": "symbol TEXT, at INTEGER, requested INTEGER, ts INTEGER, bids TEXT, "
    "asks TEXT, PRIMARY KEY(symbol,at)",
    # received/first/last: raw rows and time range actually returned (design observation),
    # even when only boundary windows are stored.
    "pages": "key TEXT PRIMARY KEY, rows INTEGER NOT NULL, received INTEGER, first INTEGER, "
    "last INTEGER",
    "identity": "key TEXT PRIMARY KEY, value TEXT NOT NULL",
    "log": "id INTEGER PRIMARY KEY, content TEXT NOT NULL",
}
HASHED = ("bars", "funding", "book_top", "book_depth")


def refuse_d98(path: Path) -> None:
    resolved = path.resolve()
    if resolved.name.lower().startswith("d98") or any(
        part.lower().startswith("d98") for part in resolved.parts[-3:-1]
    ):
        raise ValueError("D103 must not open a D98 dataset")


class Store:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        refuse_d98(path)
        self.path = path
        if readonly:
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        for table, columns in TABLES.items():
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def log(self, content: dict[str, Any]) -> None:
        with self.db:
            self.db.execute("INSERT INTO log(content) VALUES (?)", (encode(content),))

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM identity WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def bind(self, key: str, value: str) -> None:
        """Write-once identity values (allowlist digest, T0)."""
        current = self.get(key)
        if current is None:
            with self.db:
                self.db.execute("INSERT INTO identity VALUES (?,?)", (key, value))
        elif current != value:
            raise ValueError(f"dataset {key} mismatch: {current} != {value}")

    def done(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM pages WHERE key=?", (key,)).fetchone() is not None

    def put(
        self,
        table: str,
        rows: Iterable[tuple[Any, ...]],
        page: str | None = None,
        observed: tuple[int, int, int] | None = None,
    ) -> None:
        """Store rows; `observed` = (received rows, first, last) of the raw response."""
        rows = list(rows)
        received, first, last = observed if observed is not None else (len(rows), None, None)
        columns = self.db.execute(f"PRAGMA table_info({table})").fetchall()
        if any(len(row) != len(columns) for row in rows):
            raise ValueError(f"row width mismatch for {table}")
        with self.db:
            if rows:
                marks = ",".join("?" * len(columns))
                self.db.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({marks})", rows)
            if page is not None:
                self.db.execute(
                    "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?)",
                    (page, len(rows), received, first, last),
                )

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
        yield from self.db.execute(sql, params)

    def data_hash(self) -> str:
        hasher = hashlib.sha256()
        for table in HASHED:
            key = TABLES[table].split("PRIMARY KEY(")[1].rstrip(")")
            for row in self.db.execute(f"SELECT * FROM {table} ORDER BY {key}"):
                hasher.update((encode([table, *row]) + "\n").encode("utf-8"))
        return hasher.hexdigest()
