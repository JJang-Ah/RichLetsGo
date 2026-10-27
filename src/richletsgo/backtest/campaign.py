"""Pinned input snapshots and single-parent atomic campaign publication."""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from richletsgo.domain import Symbol
from richletsgo.marketdata.models import Interval, to_ms
from richletsgo.marketdata.store import DB_FILE, MarketDataStore
from richletsgo.marketdata.validate import expected_starts, funding_gaps

from .codec import digest, encode, primitive
from .wf_config import WalkConfig, utc

SCHEMA_VERSION = 1


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="pending-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def file_hash(path: Path) -> str:
    hashed = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def captures(data: dict[str, Any]) -> dict[Symbol, tuple[datetime, datetime]]:
    return {Symbol(k): (utc(v[0]), utc(v[1])) for k, v in data.items()}


def fingerprint(
    directory: Path, first: datetime, end: datetime, pinned: dict[Symbol, tuple[datetime, datetime]]
) -> str:
    hashed = hashlib.sha256()
    with closing(
        sqlite3.connect((directory / DB_FILE).resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.execute("BEGIN")
        for symbol in Symbol:
            for interval in (Interval.DAY, Interval.HOUR):
                rows = db.execute(
                    "SELECT * FROM bars WHERE symbol=? AND interval=? AND start_ms>=? AND start_ms<=? ORDER BY start_ms",
                    (
                        symbol.value,
                        interval.value,
                        to_ms(first),
                        to_ms(interval.floor(end) - interval.duration),
                    ),
                )
                for row in rows:
                    hashed.update(encode(row).encode() + b"\n")
            for row in db.execute(
                "SELECT * FROM funding WHERE symbol=? AND funding_ms>=? AND funding_ms<? ORDER BY funding_ms",
                (symbol.value, to_ms(first), to_ms(end)),
            ):
                hashed.update(encode(row).encode() + b"\n")
            for table, at in zip(("instruments", "risk_limit_tiers"), pinned[symbol], strict=True):
                for row in db.execute(
                    f"SELECT * FROM {table} WHERE symbol=? AND captured_ms=? ORDER BY rowid",
                    (symbol.value, to_ms(at)),
                ):
                    hashed.update(encode(row).encode() + b"\n")
        for row in db.execute("SELECT * FROM ingest_runs ORDER BY id"):
            hashed.update(encode(row).encode() + b"\n")
    return hashed.hexdigest()


def prepare(config: WalkConfig, commit: str, executed_at: datetime) -> tuple[Path, dict[str, Any]]:
    source = config.research.market_data_dir
    if executed_at < config.end:
        raise ValueError("campaign end must contain only closed bars")
    with MarketDataStore(source, readonly=True) as store:
        pinned = {}
        for symbol in Symbol:
            instrument = store.latest_instrument(symbol)
            tiers = store.latest_risk_tiers(symbol)
            if instrument is None or not any(t.tier == 1 for t in tiers):
                raise ValueError("instrument and tier-1 snapshots required")
            pinned[symbol] = (instrument.captured_at, tiers[0].captured_at)
    first = config.warmup_start
    # Funding look-behind may exceed indicator warm-up for a very small grid.
    funding_bounds = []
    with MarketDataStore(source, readonly=True, captured_at=pinned) as store:
        for symbol in Symbol:
            instrument = store.latest_instrument(symbol)
            assert instrument is not None
            funding_bounds.append(config.start - instrument.funding_interval)
            previous = store.load_funding(symbol, funding_bounds[-1], config.start)
            if not any(f.funding_time <= config.start for f in previous):
                raise ValueError("insufficient funding look-behind")
    funding_first = min(funding_bounds)
    hash_first = min(first, funding_first)
    data_hash = fingerprint(source, hash_first, config.end, pinned)
    identity = {
        "start": config.start,
        "end": config.end,
        "warmup_start": first,
        "hash_first": hash_first,
        "data_hash": data_hash,
        "captured_at": primitive(pinned),
        "code_commit": commit,
        "schema": SCHEMA_VERSION,
        "profile": config.research.profile,
        "close_policy": config.research.close_policy,
        "sync_policy": config.research.sync_policy,
        "grids": config.grids,
        "selection": config.research.selection,
        "pass_criteria": config.pass_criteria,
        "preregistration": config.preregistration,
        "train_months": config.train_months,
        "test_months": config.test_months,
    }
    root = config.research.results_dir / "campaigns" / digest(identity)
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["identity"] != primitive(identity):
            raise ValueError("campaign identity mismatch")
        if fingerprint(root / "input", hash_first, config.end, pinned) != data_hash:
            raise ValueError("campaign snapshot changed")
        return root, manifest
    root.mkdir(parents=True, exist_ok=True)
    snapshot = root / "input"
    snapshot.mkdir(exist_ok=True)
    # SQLite backup gives a consistent snapshot even when the source has a WAL.
    with closing(
        sqlite3.connect((source / DB_FILE).resolve().as_uri() + "?mode=ro", uri=True)
    ) as original:
        with closing(sqlite3.connect(snapshot / DB_FILE)) as copied:
            original.backup(copied)
    if fingerprint(snapshot, hash_first, config.end, pinned) != data_hash:
        raise ValueError("input changed while pinning campaign")
    issues: list[Any] = []
    with MarketDataStore(snapshot, readonly=True, captured_at=pinned) as store:
        for symbol in Symbol:
            for interval in (Interval.DAY, Interval.HOUR):
                last = interval.floor(config.end) - interval.duration
                starts = set(store.bar_starts(symbol, interval, first, last))
                for at in expected_starts(interval, first, last):
                    if at not in starts:
                        if at < config.start:
                            raise ValueError("insufficient warm-up bars")
                        issues.append(
                            {
                                "symbol": symbol.value,
                                "kind": "missing_bar:" + interval.value,
                                "first": at,
                                "last": at,
                            }
                        )
            instrument = store.latest_instrument(symbol)
            assert instrument is not None
            lo = config.start - instrument.funding_interval
            rates = store.load_funding(symbol, lo, config.end - timedelta(microseconds=1))
            for a, b in funding_gaps(
                [r.funding_time for r in rates],
                instrument.funding_interval,
                lo,
                config.end - timedelta(microseconds=1),
            ):
                issues.append(
                    {"symbol": symbol.value, "kind": "funding_gap", "first": a, "last": b}
                )
    with closing(
        sqlite3.connect((snapshot / DB_FILE).resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        runs = [
            {
                "id": r[0],
                "started_ms": r[1],
                "finished_ms": r[2],
                "summary": json.loads(r[3]),
                "ok": bool(r[4]),
                "version": r[5],
                "commit": r[6],
            }
            for r in db.execute("SELECT * FROM ingest_runs ORDER BY id")
        ]
    if not runs:
        raise ValueError("real-data campaign requires recorded ingestion provenance")
    conflicts = []
    for run in runs:
        for name, series in run["summary"].get("series", {}).items():
            if series.get("conflicts"):
                # A conflicting ingestion batch is atomic; retain its entire requested range.
                for lo, hi in series.get("requested_ranges") or [
                    (first.isoformat(), config.end.isoformat())
                ]:
                    conflicts.append({"series": name, "first": lo, "last": hi, "run": run["id"]})
    manifest = {
        "identity": primitive(identity),
        "created_at": executed_at.isoformat(),
        "ingest_runs": runs,
        "validation": primitive(issues),
        "conflicts": conflicts,
        "artifacts": {},
        "operations": [],
    }
    atomic_json(manifest_path, manifest)
    return root, manifest


def verify_inputs(root: Path, manifest: dict[str, Any], source: Path) -> None:
    identity = manifest["identity"]
    pinned = captures(identity["captured_at"])
    for directory in (source, root / "input"):
        if (
            fingerprint(directory, utc(identity["hash_first"]), utc(identity["end"]), pinned)
            != identity["data_hash"]
        ):
            raise ValueError("campaign input changed; no report or new cache publication allowed")


@contextmanager
def campaign_lock(directory: Path) -> Iterator[None]:
    """One publishing parent per output directory; OS releases the lock after a crash."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".campaign.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ValueError("another campaign parent is active") from error
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ValueError("another campaign parent is active") from error
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
