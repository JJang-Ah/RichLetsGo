"""Timestamp-only coverage decisions and separately streamed content hashes."""

import hashlib
from bisect import bisect_left, bisect_right
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any

from richletsgo.backtest.codec import digest, encode

from .boundaries import BTC_START, DAY, END, HOUR, STUDY_END, STUDY_START, design_contracts, window
from .logs import summarize_log
from .pool import active_contract
from .store import Store, dataset_lock


def missing_ranges(times: list[int], first: int, end: int, step: int) -> list[list[int]]:
    result = []
    cursor = first
    for t in times:
        if t < first or t >= end or t % step:
            continue
        if t > cursor:
            result.append([cursor, t])
        cursor = t + step
    if cursor < end:
        result.append([cursor, end])
    return result


def series_hash(
    store: Store, kind: str, symbol: str, contract: dict[str, Any] | None = None
) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    w = window(contract) if contract is not None else None
    for row in store.rows(kind, symbol):
        if (w is not None and w.contains(row[0], kind)) or (
            w is None and BTC_START <= row[0] < END and row[0] % DAY == 0
        ):
            h.update((encode(row) + "\n").encode("utf-8"))
            n += 1
    return h.hexdigest(), n


def coverage(
    store: Store, contract: dict[str, Any], siblings: list[dict[str, Any]], funding_interval: int
) -> dict[str, Any]:
    symbol = contract["symbol"]
    w = window(contract)
    all_bars = store.times("bars", symbol)
    bars = [t for t in all_bars if w.contains(t, "bars")]
    all_funding = store.times("funding", symbol)
    funding = [t for t in all_funding if w.contains(t, "funding")]
    first = next((t for t in bars if t >= contract["launch"]), None)
    la = bars[-1] if bars else None
    ea = la + HOUR if la is not None else None
    le = w.delist // HOUR * HOUR - HOUR if w.delist is not None else None
    tail = "not_applicable"
    end = w.end // HOUR * HOUR
    if le is not None and w.delist is not None and w.delist <= END and not w.empty:
        tail = "allowed" if la is not None and le - la <= DAY else "excess"
        if tail == "allowed" and ea is not None:
            end = ea
    gaps = missing_ranges(bars, w.start, max(w.start, end), HOUR)
    opportunities = eligible = insufficient = 0
    begin = max(STUDY_START, ((contract["launch"] + 4 * HOUR - 1) // (4 * HOUR)) * (4 * HOUR))
    for t in range(begin, min(STUDY_END, end), 4 * HOUR):
        if active_contract(siblings, contract["key"], t) != symbol:
            continue
        opportunities += 1
        if t - contract["launch"] < 60 * DAY or first is None or first > t - 60 * DAY:
            continue
        eligible += 1
        count = bisect_left(bars, t) - bisect_left(bars, t - 800 * HOUR)
        if count != 800 or bisect_right(funding, t) < 3:
            insufficient += 1
    intervals = Counter(b - a for a, b in zip(funding, funding[1:], strict=False))
    latest = funding[-1] - funding[-2] if len(funding) > 1 else None
    return {
        "symbol": symbol,
        "range": [w.start, w.end],
        "empty": w.empty,
        "expected_bars": max(0, (end - w.start) // HOUR),
        "actual_bars": len(bars),
        "missing_ranges": gaps,
        "launch_partial_hour": (
            {
                "start": contract["launch"] // HOUR * HOUR,
                "launch": contract["launch"],
                "missing": contract["launch"] // HOUR * HOUR not in bars,
            }
            if contract["launch"] % HOUR and w.contains(contract["launch"] // HOUR * HOUR, "bars")
            else None
        ),
        "F": first,
        "L_a": la,
        "L_e": le,
        "E_a": ea,
        "tail": tail,
        "data_end": end,
        "funding_count": len(funding),
        "funding_intervals": {str(k): v for k, v in sorted(intervals.items())},
        "current_interval_mismatch": contract["status"] == "Trading"
        and latest is not None
        and latest != funding_interval * 60_000,
        "outside_bars": len(all_bars) - len(bars),
        "outside_funding": len(all_funding) - len(funding),
        "opportunities": opportunities,
        "metadata_eligible": eligible,
        "input_insufficient": insufficient,
    }


def contract_audit(
    args: tuple[Path, dict[str, Any], list[dict[str, Any]], int],
) -> tuple[dict[str, Any], list[Any]]:
    path, c, siblings, interval = args
    store = Store(path, readonly=True)
    try:
        report = coverage(store, c, siblings, interval)
        bh, bn = series_hash(store, "bars", c["symbol"], c)
        fh, fn = series_hash(store, "funding", c["symbol"], c)
        return report, [c["symbol"], bh, fh, bn, fn]
    finally:
        store.close()


def _audit(
    pool: dict[str, Any], path: Path, *, workers: int = 1, export_log: Path | None = None
) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("workers must be positive")
    contracts = design_contracts(pool)
    metadata = {r["symbol"]: r for r in pool["body"]["instruments"]}
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in contracts.values():
        groups.setdefault(c["key"], []).append(c)
    jobs = [
        (path, c, groups[c["key"]], int(metadata[c["symbol"]]["fundingInterval"]))
        for _, c in sorted(contracts.items())
    ]
    store = Store(path, readonly=True)
    try:
        identity = store.db.execute("SELECT value FROM identity WHERE key='pool'").fetchone()
        if identity is None or identity[0] != pool["sha256"]:
            raise ValueError("database pool identity mismatch")
        if workers == 1:
            results = list(map(contract_audit, jobs))
        else:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(contract_audit, jobs))
        btc = store.times("daily", "BTCUSDT")
        btc_missing = missing_ranges(btc, BTC_START, END, DAY)
        btc_hash, btc_n = series_hash(store, "daily", "BTCUSDT")
        manifest = {
            "version": "D98-data-v1",
            "contracts": [r[1] for r in results],
            "btc_daily": [btc_hash, btc_n],
        }
        reports = [r[0] for r in results]
        eligible = sum(r["metadata_eligible"] for r in reports)
        insufficient = sum(r["input_insufficient"] for r in reports)
        status = (
            "BTC_INCOMPLETE"
            if btc_missing
            else "STUDY_UNDECIDABLE"
            if insufficient * 20 > eligible
            else "READY"
        )
        body = {
            "pool_sha256": pool["sha256"],
            "status": status,
            "contracts": reports,
            "btc_missing_ranges": btc_missing,
            "btc_outside_count": sum(not (BTC_START <= t < END and t % DAY == 0) for t in btc),
            "metadata_eligible": eligible,
            "input_insufficient": insufficient,
            "input_insufficient_ratio": str(Decimal(insufficient) / Decimal(eligible))
            if eligible
            else None,
            "manifest": manifest,
            "data_hash": digest(manifest),
            "collection_log": summarize_log(store, export_log),
        }
        return {"body": body, "sha256": digest(body)}
    finally:
        store.close()


def audit(
    pool: dict[str, Any], path: Path, *, workers: int = 1, export_log: Path | None = None
) -> dict[str, Any]:
    with dataset_lock(path):
        return _audit(pool, path, workers=workers, export_log=export_log)
