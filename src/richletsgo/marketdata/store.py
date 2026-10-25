"""SQLite store for backtest market data. Decimals are TEXT; existing rows are never overwritten."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from richletsgo.domain import Symbol

from .models import Bar, FundingRate, InstrumentSnapshot, Interval, RiskLimitTier, from_ms, to_ms

DB_FILE = "market_data.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL, interval TEXT NOT NULL, start_ms INTEGER NOT NULL,
    open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
    volume TEXT NOT NULL, turnover TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, start_ms)
);
CREATE TABLE IF NOT EXISTS funding (
    symbol TEXT NOT NULL, funding_ms INTEGER NOT NULL, rate TEXT NOT NULL,
    PRIMARY KEY (symbol, funding_ms)
);
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT NOT NULL, captured_ms INTEGER NOT NULL,
    qty_step TEXT NOT NULL, min_qty TEXT NOT NULL, tick_size TEXT NOT NULL,
    max_leverage TEXT NOT NULL, funding_interval_min INTEGER NOT NULL,
    PRIMARY KEY (symbol, captured_ms)
);
CREATE TABLE IF NOT EXISTS risk_limit_tiers (
    symbol TEXT NOT NULL, captured_ms INTEGER NOT NULL, tier INTEGER NOT NULL,
    limit_notional TEXT NOT NULL, mmr TEXT NOT NULL, imr TEXT NOT NULL,
    max_leverage TEXT NOT NULL,
    PRIMARY KEY (symbol, captured_ms, tier)
);
CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ms INTEGER NOT NULL, finished_ms INTEGER NOT NULL,
    summary_json TEXT NOT NULL, ok INTEGER NOT NULL,
    package_version TEXT NOT NULL, code_commit TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class WriteResult:
    inserted: int
    unchanged: int
    conflicts: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.conflicts


class MarketDataStore:
    """One SQLite file inside an injected directory (03 3.1). Batches are atomic:
    if any key conflicts with a stored different value, nothing in the batch is written."""

    def __init__(
        self,
        directory: Path,
        *,
        readonly: bool = False,
        captured_at: dict[Symbol, tuple[datetime, datetime]] | None = None,
    ) -> None:
        if not isinstance(directory, Path):
            raise TypeError("directory must be a pathlib.Path")
        self._captured_at = captured_at
        self.path = directory / DB_FILE
        if readonly:
            self._db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            directory.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path)
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "MarketDataStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ writes

    def _write(
        self, sql_get: str, sql_put: str, rows: Sequence[tuple[tuple[object, ...], tuple[str, ...]]]
    ) -> WriteResult:
        """rows: (key, values). Values compare as Decimal so 0.1 and 0.10 are equal."""
        inserted = unchanged = 0
        conflicts: list[str] = []
        with self._db:  # one transaction; rolled back on exception
            pending: list[tuple[object, ...]] = []
            for key, values in rows:
                stored = self._db.execute(sql_get, key).fetchone()
                if stored is None:
                    pending.append((*key, *values))
                elif all(Decimal(a) == Decimal(b) for a, b in zip(stored, values, strict=True)):
                    unchanged += 1
                else:
                    conflicts.append(repr(key))
            if conflicts:
                return WriteResult(0, unchanged, tuple(conflicts))
            self._db.executemany(sql_put, pending)
            inserted = len(pending)
        return WriteResult(inserted, unchanged, ())

    def put_bars(self, bars: Sequence[Bar]) -> WriteResult:
        rows = [
            (
                (b.symbol.value, b.interval.value, to_ms(b.start)),
                tuple(str(v) for v in (b.open, b.high, b.low, b.close, b.volume, b.turnover)),
            )
            for b in bars
        ]
        return self._write(
            "SELECT open, high, low, close, volume, turnover FROM bars "
            "WHERE symbol=? AND interval=? AND start_ms=?",
            "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )

    def put_funding(self, rates: Sequence[FundingRate]) -> WriteResult:
        rows = [((r.symbol.value, to_ms(r.funding_time)), (str(r.rate),)) for r in rates]
        return self._write(
            "SELECT rate FROM funding WHERE symbol=? AND funding_ms=?",
            "INSERT INTO funding VALUES (?,?,?)",
            rows,
        )

    def put_instrument(self, snap: InstrumentSnapshot) -> WriteResult:
        minutes = snap.funding_interval // timedelta(minutes=1)
        rows = [
            (
                (snap.symbol.value, to_ms(snap.captured_at)),
                (
                    str(snap.qty_step),
                    str(snap.min_qty),
                    str(snap.tick_size),
                    str(snap.max_leverage),
                    str(minutes),
                ),
            )
        ]
        return self._write(
            "SELECT qty_step, min_qty, tick_size, max_leverage, funding_interval_min "
            "FROM instruments WHERE symbol=? AND captured_ms=?",
            "INSERT INTO instruments VALUES (?,?,?,?,?,?,?)",
            rows,
        )

    def put_risk_tiers(self, tiers: Sequence[RiskLimitTier]) -> WriteResult:
        rows = [
            (
                (t.symbol.value, to_ms(t.captured_at), t.tier),
                (str(t.limit_notional), str(t.mmr), str(t.imr), str(t.max_leverage)),
            )
            for t in tiers
        ]
        return self._write(
            "SELECT limit_notional, mmr, imr, max_leverage FROM risk_limit_tiers "
            "WHERE symbol=? AND captured_ms=? AND tier=?",
            "INSERT INTO risk_limit_tiers VALUES (?,?,?,?,?,?,?)",
            rows,
        )

    def record_run(
        self,
        *,
        started: datetime,
        finished: datetime,
        summary: dict[str, object],
        ok: bool,
        package_version: str,
        code_commit: str,
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO ingest_runs (started_ms, finished_ms, summary_json, ok, "
                "package_version, code_commit) VALUES (?,?,?,?,?,?)",
                (
                    to_ms(started),
                    to_ms(finished),
                    json.dumps(summary, sort_keys=True, default=str),
                    int(ok),
                    package_version,
                    code_commit,
                ),
            )

    # ------------------------------------------------------------------ reads

    def bar_starts(
        self, symbol: Symbol, interval: Interval, first: datetime, last: datetime
    ) -> list[datetime]:
        cur = self._db.execute(
            "SELECT start_ms FROM bars WHERE symbol=? AND interval=? AND start_ms BETWEEN ? AND ? "
            "ORDER BY start_ms",
            (symbol.value, interval.value, to_ms(first), to_ms(last)),
        )
        return [from_ms(int(r[0])) for r in cur]

    def load_bars(
        self, symbol: Symbol, interval: Interval, first: datetime, last: datetime
    ) -> list[Bar]:
        cur = self._db.execute(
            "SELECT start_ms, open, high, low, close, volume, turnover FROM bars "
            "WHERE symbol=? AND interval=? AND start_ms BETWEEN ? AND ? ORDER BY start_ms",
            (symbol.value, interval.value, to_ms(first), to_ms(last)),
        )
        return [
            Bar(symbol, interval, from_ms(int(r[0])), *(Decimal(v) for v in r[1:])) for r in cur
        ]

    def funding_times(self, symbol: Symbol) -> list[datetime]:
        cur = self._db.execute(
            "SELECT funding_ms FROM funding WHERE symbol=? ORDER BY funding_ms", (symbol.value,)
        )
        return [from_ms(int(r[0])) for r in cur]

    def load_funding(self, symbol: Symbol, first: datetime, last: datetime) -> list[FundingRate]:
        cur = self._db.execute(
            "SELECT funding_ms, rate FROM funding WHERE symbol=? AND funding_ms BETWEEN ? AND ? "
            "ORDER BY funding_ms",
            (symbol.value, to_ms(first), to_ms(last)),
        )
        return [FundingRate(symbol, from_ms(int(r[0])), Decimal(r[1])) for r in cur]

    def latest_instrument(self, symbol: Symbol) -> InstrumentSnapshot | None:
        row = self._db.execute(
            "SELECT captured_ms, qty_step, min_qty, tick_size, max_leverage, funding_interval_min "
            "FROM instruments WHERE symbol=? AND (? IS NULL OR captured_ms=?) ORDER BY captured_ms DESC LIMIT 1",
            (
                symbol.value,
                *([to_ms(self._captured_at[symbol][0])] * 2 if self._captured_at else [None, None]),
            ),
        ).fetchone()
        if row is None:
            return None
        return InstrumentSnapshot(
            symbol,
            Decimal(row[1]),
            Decimal(row[2]),
            Decimal(row[3]),
            Decimal(row[4]),
            timedelta(minutes=int(row[5])),
            from_ms(int(row[0])),
        )

    def latest_risk_tiers(self, symbol: Symbol) -> list[RiskLimitTier]:
        latest = self._db.execute(
            "SELECT MAX(captured_ms) FROM risk_limit_tiers WHERE symbol=? AND (? IS NULL OR captured_ms=?)",
            (
                symbol.value,
                *([to_ms(self._captured_at[symbol][1])] * 2 if self._captured_at else [None, None]),
            ),
        ).fetchone()[0]
        if latest is None:
            return []
        cur = self._db.execute(
            "SELECT tier, limit_notional, mmr, imr, max_leverage FROM risk_limit_tiers "
            "WHERE symbol=? AND captured_ms=? ORDER BY tier",
            (symbol.value, latest),
        )
        return [
            RiskLimitTier(
                symbol,
                int(r[0]),
                Decimal(r[1]),
                Decimal(r[2]),
                Decimal(r[3]),
                Decimal(r[4]),
                from_ms(int(latest)),
            )
            for r in cur
        ]
