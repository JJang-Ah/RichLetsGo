"""Contract-local feature workers and deterministic chronological basket reduction."""

import hashlib
import heapq
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from richletsgo.backtest.codec import digest, encode
from richletsgo.research.d98.boundaries import DAY, STUDY_END, STUDY_START, design_contracts, window
from richletsgo.research.d98.models import D, FundingPoint, calculated
from richletsgo.research.d98.pool import active_contract
from richletsgo.research.d98.store import Store
from richletsgo.research.d101.study import bar_row

from .basket import BasketState
from .eligibility import Series
from .models import GRID, POLICY, combo_key
from .period import outcome
from .portfolio import account_period
from .statistics import summarize, verdict


@calculated
def contract_study(
    args: tuple[Path, dict[str, Any], list[dict[str, Any]], dict[str, Any], Path],
) -> dict[str, Any]:
    path, contract, siblings, coverage, output = args
    if contract["pool"] != "DESIGN" or contract["excluded_reason"] is not None:
        raise ValueError("D102 reads only non-excluded DESIGN contracts")
    bounds = window(contract)
    store = Store(path, readonly=True)
    try:
        series = Series(
            [
                bar_row(row)
                for row in store.rows("bars", contract["symbol"])
                if bounds.contains(row[0], "bars")
            ],
            [
                FundingPoint(row[0], D(row[1]))
                for row in store.rows("funding", contract["symbol"])
                if bounds.contains(row[0], "funding")
            ],
        )
    finally:
        store.close()
    stages: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    begin = max(STUDY_START, ((contract["launch"] + DAY - 1) // DAY) * DAY)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        for at in range(begin, min(STUDY_END, int(coverage["data_end"])), DAY):
            if active_contract(siblings, contract["key"], at) != contract["symbol"]:
                continue
            stages["opportunities"] += 1
            reason, features = series.classify(at, contract["launch"])
            if reason == "metadata_ineligible":
                stages[reason] += 1
                continue
            stages["metadata_eligible"] += 1
            if reason == "input_insufficient":
                stages[reason] += 1
                continue
            if reason != "eligible":
                stages["data_ineligible"] += 1
                reasons[reason] += 1
                continue
            stages["data_eligible"] += 1
            outcomes = {
                str(h): outcome(series, coverage, at, at + h * DAY)
                for h in (3, 7)
                if (at - STUDY_START) % (h * DAY) == 0 and at + h * DAY <= STUDY_END
            }
            if outcomes:
                stream.write(
                    encode(
                        {"t": at, "symbol": contract["symbol"], **features, "outcomes": outcomes}
                    )
                    + "\n"
                )
    return {"stages": dict(stages), "reasons": dict(reasons)}


def markdown(result: dict[str, Any]) -> str:
    # Normalize Decimal representations identically before and after JSON round trips.
    value = json.loads(encode(result))
    body = value["body"]
    lines = [
        "# D102 funding-neutral study",
        "",
        f"Status: **{body['status']}**",
        "",
        f"Registration: `{body['registration_digest']}`",
        f"Data hash: `{body['data_hash']}`",
        f"Body digest: `{value['sha256']}`",
        "",
        "| Combination | Valid periods | Mean | Trimmed mean | Undecidable |",
        "|---|---:|---:|---:|---|",
    ]
    for key, row in sorted(body["combinations"].items()):
        lines.append(
            f"| {key} | {row['valid']} | {row['mean']} | {row['trimmed_mean']} | {row['undecidable']} |"
        )
    lines += [
        "",
        "Detailed periods, positions, stage counts and technical metrics are in the JSON artifacts.",
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
    if workers < 1:
        raise ValueError("positive worker count required")
    contracts = design_contracts(pool)
    if (
        digest(audit["body"]) != audit["sha256"]
        or audit["body"].get("pool_sha256") != pool["sha256"]
    ):
        raise ValueError("D98 audit identity mismatch")
    if audit["body"].get("status") != "READY":
        raise ValueError("D98 audit is not READY")
    coverage = {row["symbol"]: row for row in audit["body"]["contracts"]}
    if set(coverage) != set(contracts):
        raise ValueError("D98 audit contract set mismatch")
    groups: dict[str, list[dict[str, Any]]] = {}
    for contract in contracts.values():
        groups.setdefault(contract["key"], []).append(contract)
    store = Store(path, readonly=True)
    try:
        btc = {row[0]: D(row[1]) for row in store.rows("daily", "BTCUSDT")}
    finally:
        store.close()
    output.mkdir(parents=True, exist_ok=True)
    states = {combo_key(lookback, h): BasketState() for lookback, h in GRID}
    periods: dict[str, list[dict[str, Any]]] = {key: [] for key in states}
    contributions: dict[str, list[tuple[D, int, str]]] = {key: [] for key in states}
    technical: dict[str, dict[str, Any]] = {
        key: {"delisted": 0, "worst_position": None, "liquidity_contributions": {}}
        for key in states
    }
    stages: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    artifacts: dict[str, Any] = {}
    with TemporaryDirectory(prefix="d102-contracts-", dir=output) as temp, ExitStack() as stack:
        files = [Path(temp) / f"{i}.jsonl" for i in range(len(contracts))]
        jobs = [
            (path, c, groups[c["key"]], coverage[s], file)
            for (s, c), file in zip(sorted(contracts.items()), files, strict=True)
        ]
        if workers == 1:
            results = list(map(contract_study, jobs))
        else:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(contract_study, jobs))
        for result in results:
            stages.update(result["stages"])
            reasons.update(result["reasons"])
        streams = [stack.enter_context(file.open(encoding="utf-8")) for file in files]
        pending: list[tuple[int, str, int, dict[str, Any]]] = []

        def advance(index: int) -> None:
            line = streams[index].readline()
            if line:
                row = json.loads(line)
                heapq.heappush(pending, (row["t"], row["symbol"], index, row))

        for i in range(len(streams)):
            advance(i)
        sinks = {
            name: stack.enter_context(
                (output / f"D102-study-{name}.jsonl").open("x", encoding="utf-8", newline="\n")
            )
            for name in ("periods", "positions")
        }
        hashes = {name: hashlib.sha256() for name in sinks}
        counts = dict.fromkeys(sinks, 0)

        def emit(name: str, row: dict[str, Any]) -> None:
            line = encode(row) + "\n"
            sinks[name].write(line)
            hashes[name].update(line.encode("utf-8"))
            counts[name] += 1

        for at in range(STUDY_START, STUDY_END, DAY):
            candidates = {}
            while pending and pending[0][0] == at:
                _, symbol, index, features = heapq.heappop(pending)
                candidates[symbol] = features
                advance(index)
            for lookback, h in GRID:
                end = at + h * DAY
                if (at - STUDY_START) % (h * DAY) or end > STUDY_END:
                    continue
                key = combo_key(lookback, h)
                row, positions = account_period(
                    states[key],
                    candidates,
                    lookback=lookback,
                    holding=h,
                    at=at,
                    end=end,
                    final=end + h * DAY > STUDY_END,
                    btc_return=btc[end] / btc[at] - 1 if at in btc and end in btc else None,
                )
                row["combination"] = key
                periods[key].append(row)
                emit("periods", row)
                for position in positions:
                    position["combination"] = key
                    emit("positions", position)
                    technical[key]["delisted"] += int(position["delisted"])
                    if row["kind"] != "normal" or position["invalid"] is not None:
                        continue
                    x = position["contribution"]
                    contributions[key].append((x, at, position["symbol"]))
                    worst = technical[key]["worst_position"]
                    if worst is None or (x, at, position["symbol"]) < (
                        worst["contribution"],
                        worst["t"],
                        worst["symbol"],
                    ):
                        technical[key]["worst_position"] = position
                    bucket = encode(position["slippage"])
                    bins = technical[key]["liquidity_contributions"]
                    bins[bucket] = bins.get(bucket, D(0)) + x
        if pending:
            raise ValueError("unexpected feature timestamp")
        for name, sink in sinks.items():
            sink.flush()
            file = output / f"D102-study-{name}.jsonl"
            artifacts[name] = {
                "file": file.name,
                "sha256": hashes[name].hexdigest(),
                "count": counts[name],
                "bytes": file.stat().st_size,
            }
    combinations = {key: summarize(rows, contributions[key]) for key, rows in periods.items()}
    for key, row in combinations.items():
        row["technical"].update(technical[key])
        for leg in ("long", "short"):
            row["technical"][leg + "_mean"] = (
                row["technical"][leg + "_contribution"] / row["valid"] if row["valid"] else None
            )
    body = {
        "registration_digest": registration_digest,
        "policy": POLICY,
        "pool_sha256": pool["sha256"],
        "data_hash": audit["body"]["data_hash"],
        "status": verdict(combinations, stages["metadata_eligible"], stages["input_insufficient"]),
        "stages": dict(sorted(stages.items())),
        "data_ineligible_reasons": dict(sorted(reasons.items())),
        "combinations": combinations,
        **artifacts,
    }
    result = {
        "body": body,
        "sha256": digest(body),
        "execution": {"registration_digest": registration_digest, "status": "COMPLETED"},
    }
    for name, text in (
        ("D102-study.json", encode(result) + "\n"),
        ("D102-study.md", markdown(result)),
    ):
        with (output / name).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    return result
