"""Contract-at-a-time D101 signal study with deterministic worker merging."""

import hashlib
import heapq
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from richletsgo.backtest.codec import digest, encode
from richletsgo.research.d98.boundaries import (
    BTC_START,
    DAY,
    END,
    STUDY_START,
    design_contracts,
    window,
)
from richletsgo.research.d98.btc_trend import BTCTrend
from richletsgo.research.d98.models import D, FundingPoint, RawBar, calculated
from richletsgo.research.d98.pool import active_contract
from richletsgo.research.d98.store import Store

from .models import GRID, POLICY, SIGNAL_END
from .signals import Series
from .simulate import simulate
from .statistics import Aggregate, daily_holding_distribution, verdict


def bar_row(row: list[Any]) -> RawBar:
    from richletsgo.research.d98.models import D

    return RawBar(row[0], *(D(value) for value in row[1:]))


def _first_daily_at_or_after(at: int) -> int:
    return ((at + DAY - 1) // DAY) * DAY


def can_evaluate(at: int, busy_until: int | None, *, invalid_exit: bool = False) -> bool:
    """Valid exits free a position at the same t; invalid paths resume after their gap."""
    if busy_until is None:
        return True
    return at > busy_until if invalid_exit else at >= busy_until


@calculated
def contract_study(
    args: tuple[
        Path,
        dict[str, Any],
        list[dict[str, Any]],
        dict[str, Any],
        BTCTrend,
        Path,
    ],
) -> dict[str, Any]:
    path, contract, siblings, coverage, btc, output = args
    if contract["pool"] != "DESIGN" or contract["excluded_reason"] is not None:
        raise ValueError("D101 reads only non-excluded DESIGN contracts")
    store = Store(path, readonly=True)
    data_window = window(contract)
    try:
        series = Series(
            [
                bar_row(row)
                for row in store.rows("bars", contract["symbol"])
                if data_window.contains(row[0], "bars")
            ],
            [
                FundingPoint(row[0], Decimal(row[1]))
                for row in store.rows("funding", contract["symbol"])
                if data_window.contains(row[0], "funding")
            ],
        )
    finally:
        store.close()

    stages: Counter[str] = Counter(
        {
            "opportunities": 0,
            "metadata_ineligible": 0,
            "metadata_eligible": 0,
            "input_insufficient": 0,
            "data_ineligible": 0,
            "data_eligible": 0,
        }
    )
    data_reasons: Counter[str] = Counter()
    counts: dict[str, dict[str, Any]] = {
        combo.key: {"fired": 0, "invalid": Counter()} for combo in GRID
    }
    busy_until: dict[str, int | None] = {combo.key: None for combo in GRID}
    invalid_exit: dict[str, bool] = {combo.key: False for combo in GRID}
    begin = max(STUDY_START, _first_daily_at_or_after(contract["launch"]))
    end = min(SIGNAL_END, int(coverage["data_end"]))
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        for at in range(begin, end, DAY):
            if active_contract(siblings, contract["key"], at) != contract["symbol"]:
                continue
            stages["opportunities"] += 1
            if (
                at - contract["launch"] < 60 * DAY
                or coverage.get("F") is None
                or int(coverage["F"]) > at - 60 * DAY
            ):
                stages["metadata_ineligible"] += 1
                continue
            stages["metadata_eligible"] += 1
            if not series.input_complete(at):
                stages["input_insufficient"] += 1
                continue
            features, reason = series.features(at)
            if reason is not None:
                stages["data_ineligible"] += 1
                data_reasons[reason] += 1
                continue
            stages["data_eligible"] += 1
            state = btc.at(at)
            btc_label = "LONG" if state == 1 else "SHORT" if state == -1 else "FLAT"

            # Stages 1-4 are counted above for every t, before position suppression.
            for combo in GRID:
                key = combo.key
                if not can_evaluate(at, busy_until[key], invalid_exit=invalid_exit[key]):
                    continue
                busy_until[key] = None
                invalid_exit[key] = False
                direction = series.entry_direction(at, combo)
                if direction == 0:
                    continue
                counts[key]["fired"] += 1
                trade, invalid_reason, release_at = simulate(
                    series,
                    coverage,
                    contract["symbol"],
                    at,
                    direction,
                    combo,
                    features,
                    btc_label,
                )
                busy_until[key] = release_at
                invalid_exit[key] = invalid_reason is not None
                if invalid_reason is not None:
                    counts[key]["invalid"][invalid_reason] += 1
                elif trade is not None:
                    trade["liquidity_bucket"] = str(features["liquidity_bucket"])
                    stream.write(encode(trade) + "\n")
                else:
                    raise ValueError("simulator returned neither a trade nor an invalid reason")

    if stages["opportunities"] != stages["metadata_ineligible"] + stages["metadata_eligible"]:
        raise ValueError("metadata stage counts disagree")
    if stages["metadata_eligible"] != (
        stages["input_insufficient"] + stages["data_ineligible"] + stages["data_eligible"]
    ):
        raise ValueError("input/data stage counts disagree")
    return {
        "symbol": contract["symbol"],
        "stages": dict(stages),
        "data_ineligible_reasons": dict(data_reasons),
        "combinations": counts,
    }


def trade_key(row: dict[str, Any]) -> tuple[int, str, str]:
    return int(row["t"]), str(row["symbol"]), str(row["combination"])


def _write_lf(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def markdown(result: dict[str, Any]) -> str:
    body = result["body"]
    lines = [
        "# D101 S1-B signal study",
        "",
        f"Status: **{body['status']}**",
        "",
        f"Registration: `{body['registration_digest']}`",
        f"Data hash: `{body['data_hash']}`",
        f"Body digest: `{result['sha256']}`",
        "",
        "## Stage counts",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(body["stages"].items()))
    lines += [
        "",
        "## Combinations",
        "",
        "| Combination | Fired | Valid | Mean R | Trimmed mean R | Undecidable |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for key, row in sorted(body["combinations"].items()):
        lines.append(
            f"| {key} | {row['signals']} | {row['valid']} | {row['mean_R']} | "
            f"{row['trimmed_mean_R']} | {row['undecidable']} |"
        )
    lines += [
        "",
        "## Technical distributions",
        "",
        "Daily concurrent holdings is the maximum number of overlapping `[entry, exit)` "
        "trade intervals in each UTC date; it is technical only.",
        "",
        f"Trades SHA-256: `{body['trades']['sha256']}`",
        "",
    ]
    return "\n".join(lines)


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
    """Run the deterministic D101 study against a supplied (normally synthetic) dataset."""
    from richletsgo.backtest.codec import digest as content_digest

    if workers < 1:
        raise ValueError("positive worker count required")
    contracts = design_contracts(pool)
    if content_digest(audit.get("body", {})) != audit.get("sha256"):
        raise ValueError("D98 audit digest mismatch")
    if audit["body"].get("pool_sha256") != pool["sha256"]:
        raise ValueError("D98 audit pool mismatch")
    if audit["body"].get("status") != "READY":
        raise ValueError("D98 data audit is not READY")
    coverage_rows = {row["symbol"]: row for row in audit["body"]["contracts"]}
    if set(coverage_rows) != set(contracts):
        raise ValueError("D98 audit contract set mismatch")

    store = Store(path, readonly=True)
    try:
        btc = BTCTrend(
            [
                bar_row(row)
                for row in store.rows("daily", "BTCUSDT")
                if BTC_START <= row[0] < END and row[0] % DAY == 0
            ]
        )
    finally:
        store.close()

    groups: dict[str, list[dict[str, Any]]] = {}
    for contract in contracts.values():
        groups.setdefault(contract["key"], []).append(contract)
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="d101-contracts-", dir=output) as temp:
        temporary = Path(temp)
        files = [temporary / f"{index}.jsonl" for index in range(len(contracts))]
        jobs = [
            (path, contract, groups[contract["key"]], coverage_rows[symbol], btc, file)
            for (symbol, contract), file in zip(sorted(contracts.items()), files, strict=True)
        ]
        if workers == 1:
            results = list(map(contract_study, jobs))
        else:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(contract_study, jobs))

        stages: Counter[str] = Counter()
        ineligible: Counter[str] = Counter()
        for row in results:
            stages.update(row["stages"])
            ineligible.update(row["data_ineligible_reasons"])
        totals: dict[str, dict[str, Any]] = {
            combo.key: {"fired": 0, "invalid": Counter()} for combo in GRID
        }
        for combo in GRID:
            for row in results:
                totals[combo.key]["fired"] += row["combinations"][combo.key]["fired"]
                totals[combo.key]["invalid"].update(row["combinations"][combo.key]["invalid"])
        aggregates = {
            combo.key: Aggregate(totals[combo.key]["fired"], dict(totals[combo.key]["invalid"]))
            for combo in GRID
        }
        holding_events: list[tuple[int, int]] = []
        trade_hash = hashlib.sha256()
        trade_count = 0
        streams = [file.open(encoding="utf-8", newline="") for file in files]
        try:
            pending: list[tuple[tuple[int, str, str], int, dict[str, Any]]] = []
            for index, stream in enumerate(streams):
                line = stream.readline()
                if line:
                    row = json.loads(line)
                    heapq.heappush(pending, (trade_key(row), index, row))
            with (output / "D101-study-trades.jsonl").open(
                "x", encoding="utf-8", newline="\n"
            ) as target:
                while pending:
                    _, index, row = heapq.heappop(pending)
                    line = encode(row) + "\n"
                    target.write(line)
                    trade_hash.update(line.encode("utf-8"))
                    trade_count += 1
                    aggregates[row["combination"]].add(row)
                    entry_at, exit_at = int(row["t"]), int(row["exit_time"])
                    if exit_at > entry_at:
                        holding_events.extend(((entry_at, 1), (exit_at, -1)))
                    line = streams[index].readline()
                    if line:
                        next_row = json.loads(line)
                        heapq.heappush(pending, (trade_key(next_row), index, next_row))
        finally:
            for stream in streams:
                stream.close()

        combinations = {key: value.result() for key, value in sorted(aggregates.items())}
        eligible = stages["metadata_eligible"]
        insufficient = stages["input_insufficient"]
        body = {
            "registration_digest": registration_digest,
            "policy": POLICY,
            "pool_sha256": pool["sha256"],
            "data_hash": audit["body"]["data_hash"],
            "status": verdict(combinations, eligible, insufficient),
            "stages": dict(sorted(stages.items())),
            "input_insufficient_ratio": (str(D(insufficient) / D(eligible)) if eligible else None),
            "data_ineligible_reasons": dict(sorted(ineligible.items())),
            "combinations": combinations,
            "daily_concurrent_holdings": daily_holding_distribution(
                holding_events, start=STUDY_START, end=SIGNAL_END + 90 * DAY
            ),
            "trades": {
                "file": "D101-study-trades.jsonl",
                "sha256": trade_hash.hexdigest(),
                "count": trade_count,
                "bytes": (output / "D101-study-trades.jsonl").stat().st_size,
                "commit_eligible": (output / "D101-study-trades.jsonl").stat().st_size
                <= 10_000_000,
            },
        }
        result = {
            "body": body,
            "execution": {
                "registration_digest": registration_digest,
                "status": "COMPLETED",
            },
            "sha256": digest(body),
        }
        _write_lf(output / "D101-study.json", encode(result) + "\n")
        _write_lf(output / "D101-study.md", markdown(result))
        return result
