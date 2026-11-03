"""Contract-at-a-time synthetic/registered study engine with deterministic trade streams."""

import hashlib
import heapq
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from richletsgo.backtest.codec import digest, encode

from .boundaries import BTC_START, DAY, END, HOUR, STUDY_END, STUDY_START, design_contracts, window
from .btc_trend import BTCTrend
from .models import GRID, POLICY, D, FundingPoint, RawBar, calculated
from .pool import active_contract
from .signals import Series
from .simulate import simulate
from .statistics import Aggregate, verdict
from .store import Store


def bar_row(row: list[Any]) -> RawBar:
    return RawBar(row[0], *(D(v) for v in row[1:]))


@calculated
def contract_study(
    args: tuple[Path, dict[str, Any], list[dict[str, Any]], dict[str, Any], BTCTrend, Path],
) -> dict[str, Any]:
    path, c, siblings, cov, trend, output = args
    if c["pool"] != "DESIGN" or c["excluded_reason"] is not None:
        raise ValueError("DESIGN only")
    store = Store(path, readonly=True)
    w = window(c)
    try:
        series = Series(
            [bar_row(r) for r in store.rows("bars", c["symbol"]) if w.contains(r[0], "bars")],
            [
                FundingPoint(r[0], D(r[1]))
                for r in store.rows("funding", c["symbol"])
                if w.contains(r[0], "funding")
            ],
        )
    finally:
        store.close()
    stages = Counter(
        {
            "opportunities": 0,
            "metadata_ineligible": 0,
            "metadata_eligible": 0,
            "input_insufficient": 0,
            "data_ineligible": 0,
            "data_eligible": 0,
        }
    )
    reasons: Counter[str] = Counter()
    counts: dict[str, dict[str, Any]] = {
        g.key: {"fired": 0, "valid": 0, "invalid": Counter()} for g in GRID
    }
    begin = max(STUDY_START, ((c["launch"] + 4 * HOUR - 1) // (4 * HOUR)) * 4 * HOUR)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        for t in range(begin, min(STUDY_END, cov["data_end"]), 4 * HOUR):
            if active_contract(siblings, c["key"], t) != c["symbol"]:
                continue
            stages["opportunities"] += 1
            if t - c["launch"] < 60 * DAY or cov["F"] is None or cov["F"] > t - 60 * DAY:
                stages["metadata_ineligible"] += 1
                continue
            stages["metadata_eligible"] += 1
            if not series.input_complete(t):
                stages["input_insufficient"] += 1
                continue
            features, reason = series.features(t)
            if reason:
                stages["data_ineligible"] += 1
                reasons[reason] += 1
                continue
            stages["data_eligible"] += 1
            direction = trend.at(t)
            matching = {
                (v, n): series.signal(t, direction, v, n, features)
                for v in (2, 3)
                for n in (42, 84)
            }
            for combo in GRID:
                if not matching[combo.v, combo.n]:
                    continue
                counter = counts[combo.key]
                counter["fired"] += 1
                trade, reason = simulate(
                    series, trend, cov, c["symbol"], t, direction, combo, features
                )
                if reason:
                    counter["invalid"][reason] += 1
                else:
                    counter["valid"] += 1
                    stream.write(encode(trade) + "\n")
    for key in ("opportunities", "metadata_eligible", "input_insufficient"):
        if stages[key] != cov[key]:
            raise ValueError(f"audit stage mismatch: {key}")
    return {
        "symbol": c["symbol"],
        "stages": dict(stages),
        "data_reasons": dict(reasons),
        "combinations": counts,
    }


def trade_key(row: dict[str, Any]) -> tuple[int, str, str]:
    return row["t"], row["symbol"], row["combination"]


@calculated
def study(
    pool: dict[str, Any],
    audit: dict[str, Any],
    path: Path,
    output: Path,
    *,
    workers: int,
    registration_digest: str,
) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("positive worker count required")
    contracts = design_contracts(pool)
    if digest(audit["body"]) != audit["sha256"] or audit["body"]["pool_sha256"] != pool["sha256"]:
        raise ValueError("audit identity mismatch")
    if audit["body"]["status"] != "READY":
        raise ValueError(audit["body"]["status"])
    coverage = {c["symbol"]: c for c in audit["body"]["contracts"]}
    if set(coverage) != set(contracts):
        raise ValueError("audit contract mismatch")
    store = Store(path, readonly=True)
    try:
        trend = BTCTrend(
            [
                bar_row(r)
                for r in store.rows("daily", "BTCUSDT")
                if BTC_START <= r[0] < END and r[0] % DAY == 0
            ]
        )
    finally:
        store.close()
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in contracts.values():
        groups.setdefault(c["key"], []).append(c)
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="d98-contracts-", dir=output) as temp:
        files = [Path(temp) / f"{i}.jsonl" for i in range(len(contracts))]
        jobs = [
            (path, c, groups[c["key"]], coverage[s], trend, f)
            for (s, c), f in zip(sorted(contracts.items()), files, strict=True)
        ]
        if workers == 1:
            results = list(map(contract_study, jobs))
        else:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(contract_study, jobs))
        stages: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        for r in results:
            stages.update(r["stages"])
            reasons.update(r["data_reasons"])
        aggregate = {
            g.key: Aggregate(sum(r["combinations"][g.key]["valid"] for r in results)) for g in GRID
        }
        trades_hash = hashlib.sha256()
        trades_count = 0
        # At most one pending record per contract, not all trades in memory.
        streams = [f.open(encoding="utf-8") for f in files]
        try:
            pending: list[tuple[tuple[int, str, str], int, dict[str, Any]]] = []
            for i, stream in enumerate(streams):
                line = stream.readline()
                if line:
                    row = json.loads(line)
                    heapq.heappush(pending, (trade_key(row), i, row))
            with (output / "D98-study-trades.jsonl").open(
                "x", encoding="utf-8", newline="\n"
            ) as target:
                while pending:
                    _, i, row = heapq.heappop(pending)
                    line = encode(row) + "\n"
                    target.write(line)
                    trades_hash.update(line.encode("utf-8"))
                    trades_count += 1
                    aggregate[row["combination"]].add(row)
                    line = streams[i].readline()
                    if line:
                        row = json.loads(line)
                        heapq.heappush(pending, (trade_key(row), i, row))
        finally:
            for stream in streams:
                stream.close()
        combos = {}
        for g in GRID:
            invalid: Counter[str] = Counter()
            for r in results:
                invalid.update(r["combinations"][g.key]["invalid"])
            combos[g.key] = aggregate[g.key].result(
                sum(r["combinations"][g.key]["fired"] for r in results), dict(invalid)
            )
    for key in ("metadata_eligible", "input_insufficient"):
        if stages[key] != audit["body"][key]:
            raise ValueError("aggregate audit count mismatch")
    body = {
        "registration_digest": registration_digest,
        "policy": POLICY,
        "data_hash": audit["body"]["data_hash"],
        "status": verdict(combos, stages["metadata_eligible"], stages["input_insufficient"]),
        "stages": dict(sorted(stages.items())),
        "data_ineligible_reasons": dict(sorted(reasons.items())),
        "combinations": combos,
        "trades": {
            "file": "D98-study-trades.jsonl",
            "sha256": trades_hash.hexdigest(),
            "count": trades_count,
            "bytes": (output / "D98-study-trades.jsonl").stat().st_size,
            "commit_eligible": (output / "D98-study-trades.jsonl").stat().st_size <= 10_000_000,
        },
    }
    return {"body": body, "sha256": digest(body)}
