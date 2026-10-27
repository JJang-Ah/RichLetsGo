"""Walk-forward CLI: pinned public inputs, joint selection, bounded process pool."""

import argparse
import asyncio
import json
import os
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from richletsgo.domain import Symbol
from richletsgo.marketdata.models import Interval
from richletsgo.marketdata.store import MarketDataStore
from richletsgo.strategy.candidates import required_bars

from .campaign import (
    SCHEMA_VERSION,
    atomic_json,
    campaign_lock,
    captures,
    file_hash,
    prepare,
    verify_inputs,
)
from .codec import digest, primitive
from .results import BacktestResult
from .runner import BacktestRun
from .statistics import aggregate, choose, summarize
from .wf_config import WalkConfig, load_walk_config, parameter_key, utc


class ComputeLimit(ValueError):
    pass


@dataclass(frozen=True)
class Job:
    root: str
    combo: str
    pair_index: int
    start: str
    end: str
    kind: str
    key: str
    commit: str
    executed_at: str
    data_hash: str
    pinned: dict[str, Any]
    conflicts: list[dict[str, Any]]
    units: Decimal


def run_for(
    config: WalkConfig,
    combo: str,
    pair_index: int,
    start: datetime,
    end: datetime,
    commit: str,
    executed_at: datetime,
) -> BacktestRun:
    s1, s2 = config.grids[combo][pair_index]
    return BacktestRun(
        start,
        end,
        s1,
        s2,
        config.research.profile,
        config.research.close_policy,
        config.research.sync_policy,
        tuple(Symbol),
        executed_at,
        commit,
    )


def make_job(
    config: WalkConfig,
    root: Path,
    manifest: dict[str, Any],
    combo: str,
    pair_index: int,
    start: datetime,
    end: datetime,
    kind: str,
    units: Decimal,
) -> Job:
    identity = manifest["identity"]
    run = run_for(
        config, combo, pair_index, start, end, identity["code_commit"], utc(manifest["created_at"])
    )
    normalized = primitive(run)
    normalized.pop("executed_at")
    key = digest(
        [digest(normalized), identity["data_hash"], identity["code_commit"], kind, SCHEMA_VERSION]
    )
    return Job(
        str(root),
        combo,
        pair_index,
        start.isoformat(),
        end.isoformat(),
        kind,
        key,
        identity["code_commit"],
        manifest["created_at"],
        identity["data_hash"],
        identity["captured_at"],
        manifest["conflicts"],
        units,
    )


def worker(job: Job) -> dict[str, Any]:
    started = time.perf_counter()
    root = Path(job.root)
    config = load_walk_config(root / "config.toml")
    run = run_for(
        config,
        job.combo,
        job.pair_index,
        utc(job.start),
        utc(job.end),
        job.commit,
        utc(job.executed_at),
    )
    before = max(timedelta(days=required_bars(run.s1)), timedelta(hours=required_bars(run.s2)))
    conflicts = [
        c
        for c in job.conflicts
        if utc(c["first"]) < run.end and utc(c["last"]) >= Interval.DAY.floor(run.start - before)
    ]
    output = root / "artifacts" / job.key
    if conflicts:
        normalized = primitive(run)
        normalized.pop("executed_at")
        result = BacktestResult(
            "INVALID_DATA", tuple(conflicts), (), (), (), (), (), (), (), {"config": normalized}, ()
        )
        output.mkdir(parents=True, exist_ok=True)
    else:
        with MarketDataStore(
            root / "input", readonly=True, captured_at=captures(job.pinned)
        ) as store:
            result = asyncio.run(
                run.execute(
                    store,
                    results_dir=output,
                    journal_dir=root / "journal",
                    storage_policy=job.kind,
                    input_key=job.key,
                )
            )
    summary = summarize(result, job.key)
    payload: dict[str, Any] = {"summary": summary, "kind": job.kind, "schema": SCHEMA_VERSION}
    if job.kind == "FULL":
        payload.update(trades=primitive(result.trades), equity=primitive(result.equity))
    files = []
    if job.kind == "FULL":
        full = output / (result.result_hash + ".sqlite3")
        if conflicts:
            result.save(output)
        files.append({"path": str(full.relative_to(root)), "sha256": file_hash(full)})
    atomic_json(output / "payload.json", payload)
    return {
        "key": job.key,
        "path": str((output / "payload.json").relative_to(root)),
        "sha256": file_hash(output / "payload.json"),
        "status": result.status,
        "files": files,
        "seconds": str(time.perf_counter() - started),
        "units": str(job.units),
    }


def cached(root: Path, manifest: dict[str, Any], job: Job) -> dict[str, Any] | None:
    entry = manifest["artifacts"].get(job.key)
    if not entry or entry["status"] not in ("COMPLETED", "INVALID_DATA"):
        return None
    path = (root / entry["path"]).resolve()
    if (
        not path.is_relative_to(root.resolve())
        or not path.is_file()
        or file_hash(path) != entry["sha256"]
    ):
        return None
    for artifact in entry.get("files", []):
        full = (root / artifact["path"]).resolve()
        if (
            not full.is_relative_to(root.resolve())
            or not full.is_file()
            or file_hash(full) != artifact["sha256"]
        ):
            return None
    if job.kind == "FULL" and not entry.get("files"):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload["schema"] != SCHEMA_VERSION
            or payload["kind"] != job.kind
            or payload["summary"]["input_key"] != job.key
            or payload["summary"]["status"] != entry["status"]
            or payload["summary"]["remaining_positions"]
        ):
            return None
        return dict(payload)
    except (KeyError, ValueError, TypeError):
        return None


def estimate(units: Decimal, seconds: Decimal, workers: int, efficiency: Decimal) -> dict[str, str]:
    work = units * seconds
    return {"work_seconds": str(work), "elapsed_hours": str(work / (workers * efficiency * 3600))}


def execute_jobs(
    config: WalkConfig,
    root: Path,
    manifest: dict[str, Any],
    jobs: list[Job],
    reserve: Decimal,
    *,
    max_new_runs: int | None = None,
) -> dict[str, dict[str, Any]]:
    verify_inputs(root, manifest, config.research.market_data_dir)
    results = {}
    pending = []
    for job in jobs:
        old = cached(root, manifest, job)
        if old is None:
            pending.append(job)
        else:
            results[job.key] = old
    if not pending:
        return results
    rate = config.per_run_seconds_12m
    measure: list[tuple[Decimal, Decimal]] = []
    operation: dict[str, Any] = {
        "workers": config.workers,
        "reused": len(results),
        "new": 0,
        "planned_pending": len(pending),
        "reserved_test_units": str(reserve),
        "measured_seconds": "0",
        "measurement_source": config.measurement_source,
        "estimates": [],
    }
    manifest["operations"].append(operation)

    def check(rest: list[Job]) -> None:
        plan = estimate(
            sum((j.units for j in rest), reserve), rate, config.workers, config.parallel_efficiency
        )
        operation["estimates"].append(plan)
        atomic_json(root / "manifest.json", manifest)
        print("PLAN " + json.dumps(plan), flush=True)
        if Decimal(plan["elapsed_hours"]) > config.max_compute_hours:
            raise ComputeLimit("remaining estimated elapsed time exceeds max_compute_hours")

    check(pending)
    with ProcessPoolExecutor(max_workers=config.workers) as pool:
        offset = 0
        while offset < len(pending):
            count = min(config.workers, len(pending) - offset)
            if max_new_runs is not None:
                count = min(count, max_new_runs - operation["new"])
                if count <= 0:
                    raise ComputeLimit("requested checkpoint after max_new_runs")
            batch = pending[offset : offset + count]
            futures = {pool.submit(worker, job): job for job in batch}
            for future in as_completed(futures):
                entry = future.result()
                job = futures[future]
                verify_inputs(root, manifest, config.research.market_data_dir)
                # Only this parent publishes the cache manifest, after input verification.
                manifest["artifacts"][job.key] = entry
                operation["new"] += 1
                measure.append((Decimal(entry["seconds"]), job.units))
                operation["measured_seconds"] = str(sum((v for v, _ in measure), Decimal(0)))
                atomic_json(root / "manifest.json", manifest)
                payload = cached(root, manifest, job)
                if payload is None:
                    raise ValueError("execution incomplete or artifact failed validation")
                results[job.key] = payload
                print(
                    f"DONE {job.combo} {job.kind} {job.start} {job.pair_index} {entry['status']}",
                    flush=True,
                )
            offset += count
            if len(measure) >= config.reestimate_after and offset < len(pending):
                rate = sum((s for s, _ in measure), Decimal(0)) / sum(
                    (u for _, u in measure), Decimal(0)
                )
                check(pending[offset:])
    return results


def run_campaign(
    config_path: Path,
    commit: str,
    executed_at: datetime,
    *,
    prepare_only: bool = False,
    max_new_runs: int | None = None,
) -> Path:
    config = load_walk_config(config_path)
    if (
        "S1B-S2P" in config.grids
        and config.start < utc("2023-09-01T00:00:00Z")
        and config.end > utc("2021-06-01T00:00:00Z")
        and config.preregistration is None
    ):
        raise ValueError("D96 requires published preregistration before accessing holdout inputs")
    with campaign_lock(config.research.results_dir):
        return _run_campaign(
            config_path,
            config,
            commit,
            executed_at,
            prepare_only=prepare_only,
            max_new_runs=max_new_runs,
        )


def _run_campaign(
    config_path: Path,
    config: WalkConfig,
    commit: str,
    executed_at: datetime,
    *,
    prepare_only: bool,
    max_new_runs: int | None,
) -> Path:
    root, manifest = prepare(config, commit, executed_at)
    # Original config remains provenance; relative paths are never used by workers.
    frozen = root / "config.toml"
    if not frozen.exists():
        frozen.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    if "config_file_hash" not in manifest:
        manifest["config_file_hash"] = file_hash(frozen)
        atomic_json(root / "manifest.json", manifest)
    if file_hash(frozen) != manifest["config_file_hash"]:
        raise ValueError("frozen campaign configuration changed")
    verify_inputs(root, manifest, config.research.market_data_dir)
    print(f"CAMPAIGN {root}", flush=True)
    if prepare_only:
        return root
    started = time.perf_counter()
    training = [
        make_job(
            config,
            root,
            manifest,
            combo,
            index,
            w.train_start,
            w.train_end,
            "SUMMARY",
            Decimal(config.train_months) / 12,
        )
        for combo, pairs in sorted(config.grids.items())
        for w in config.windows
        for index in range(len(pairs))
    ]
    reserve = Decimal(len(config.windows) * len(config.grids) * config.test_months) / 12
    trained = execute_jobs(config, root, manifest, training, reserve, max_new_runs=max_new_runs)
    windows: dict[str, list[dict[str, Any]]] = {c: [] for c in sorted(config.grids)}
    testing = []
    for combo, pairs in sorted(config.grids.items()):
        for window in config.windows:
            jobs = [
                j
                for j in training
                if j.combo == combo and j.start == window.train_start.isoformat()
            ]
            rows = [
                (
                    parameter_key(pairs[j.pair_index][0]) + parameter_key(pairs[j.pair_index][1]),
                    trained[j.key]["summary"],
                )
                for j in jobs
            ]
            winner = choose(
                [r for r in rows if Decimal(r[1]["net_pnl"]) > 0]
                if config.research.selection.get("require_positive_score", False)
                else rows,
                config.drawdown_floor,
                config.minimum_trades,
            )
            invalid = any(r["status"] == "INVALID_DATA" for _, r in rows)
            row: dict[str, Any] = {
                "index": window.index,
                "start": window.train_end.isoformat(),
                "end": window.end.isoformat(),
                "status": "INVALID_DATA" if invalid else "NO_CANDIDATE",
                "selected": None,
                "training": [
                    {
                        "parameters": primitive(pairs[j.pair_index]),
                        "key": j.key,
                        "summary": trained[j.key]["summary"],
                    }
                    for j in jobs
                ],
            }
            if winner is not None and not invalid:
                selected = next(j for j in jobs if trained[j.key]["summary"] is winner[1])
                job = make_job(
                    config,
                    root,
                    manifest,
                    combo,
                    selected.pair_index,
                    window.train_end,
                    window.end,
                    "FULL",
                    Decimal(config.test_months) / 12,
                )
                testing.append(job)
                row.update(
                    selected=primitive(pairs[selected.pair_index]),
                    key=job.key,
                    selection_score=str(
                        Decimal(winner[1]["net_pnl"])
                        / max(Decimal(winner[1]["max_drawdown"]), config.drawdown_floor)
                    ),
                )
            windows[combo].append(row)
    tested = execute_jobs(config, root, manifest, testing, Decimal(0), max_new_runs=max_new_runs)
    for window_rows in windows.values():
        for row in window_rows:
            if "key" in row:
                payload = tested[row["key"]]
                row.update(
                    status=payload["summary"]["status"],
                    summary=payload["summary"],
                    trades=payload["trades"],
                    equity=payload["equity"],
                )
    verify_inputs(root, manifest, config.research.market_data_dir)
    aggregates = {
        c: aggregate(
            rows,
            config.research.profile.start_equity,
            config.max_invalid_ratio,
            config.pass_criteria,
        )
        for c, rows in windows.items()
    }
    body = {
        "identity": manifest["identity"],
        "manifest_hash": digest(manifest["identity"]),
        "ingest_runs": manifest["ingest_runs"],
        "validation": manifest["validation"],
        "conflicts": manifest["conflicts"],
        "windows": windows,
        "aggregates": aggregates,
    }
    atomic_json(root / "report-data.json", body)
    manifest["last_elapsed_seconds"] = str(time.perf_counter() - started)
    atomic_json(root / "manifest.json", manifest)
    from .wf_report import render

    report = render(body, manifest)
    temporary = root / "report.pending.md"
    temporary.write_text(report, encoding="utf-8")
    os.replace(temporary, root / "report.md")
    print(f"REPORT {root / 'report.md'}", flush=True)
    return root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--commit")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--max-new-runs", type=int)
    args = parser.parse_args()
    commit = args.commit or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    run_campaign(
        args.config,
        commit,
        datetime.now(UTC),
        prepare_only=args.prepare_only,
        max_new_runs=args.max_new_runs,
    )


if __name__ == "__main__":
    main()
