"""D102 synthetic benchmark; reuse D101 DB builder and Windows RSS sampler."""

import argparse
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from benchmark_d101_study import _build_database, _PeakMemory, _synthetic_pool

from richletsgo.backtest.codec import digest
from richletsgo.research.d98.boundaries import HOUR, START
from richletsgo.research.d102.study import study


def run_benchmark(*, contract_count: int, hourly_bars: int, workers: int) -> dict[str, Any]:
    contracts = [
        {
            "symbol": f"SYN{index:04d}USDT",
            "key": f"SYN{index:04d}",
            "launch": START,
            "delist": None,
            "status": "Trading",
            "pool": "DESIGN",
            "excluded_reason": None,
            "priority": [1, START, f"SYN{index:04d}USDT"],
        }
        for index in range(contract_count)
    ]
    pool = _synthetic_pool(contracts)
    data_end = START + hourly_bars * HOUR
    audit_body = {
        "pool_sha256": pool["sha256"],
        "status": "READY",
        "data_hash": digest(["D102-synthetic-benchmark-v1", contract_count, hourly_bars]),
        "contracts": [
            {
                "symbol": contract["symbol"],
                "F": START,
                "data_end": data_end,
                "tail": "not_applicable",
                "L_a": data_end - HOUR,
            }
            for contract in contracts
        ],
    }
    audit = {"body": audit_body, "sha256": digest(audit_body)}
    temp_directory_name = "d102-synthetic-benchmark-"
    started = time.perf_counter()
    with TemporaryDirectory(prefix=temp_directory_name) as temp:
        temporary_root = Path(temp)
        database = temporary_root / "synthetic.sqlite"
        with _PeakMemory() as memory:
            _build_database(database, contracts, hourly_bars)
        build_seconds = time.perf_counter() - started
        db_bytes = database.stat().st_size

        runs = []
        for worker_count in (1, workers):
            output = temporary_root / f"output-{worker_count}"
            begin = time.perf_counter()
            with _PeakMemory() as study_memory:
                result = study(
                    pool,
                    audit,
                    database,
                    output,
                    workers=worker_count,
                    registration_digest="synthetic-registration",
                )
            runs.append(
                {
                    "workers": worker_count,
                    "elapsed_seconds": round(time.perf_counter() - begin, 3),
                    "peak_process_tree_working_set_bytes": study_memory.peak_bytes,
                    "body_sha256": result["sha256"],
                    "positions_sha256": result["body"]["positions"]["sha256"],
                    "position_count": result["body"]["positions"]["count"],
                    "position_bytes": result["body"]["positions"]["bytes"],
                }
            )
        same_digest = runs[0]["body_sha256"] == runs[1]["body_sha256"]
        same_positions = (
            runs[0]["positions_sha256"] == runs[1]["positions_sha256"]
            and (temporary_root / "output-1" / "D102-study-positions.jsonl").read_bytes()
            == (temporary_root / f"output-{workers}" / "D102-study-positions.jsonl").read_bytes()
        )
        if not same_digest or not same_positions:
            raise AssertionError("single-worker and multi-worker outputs differ")
        result = {
            "benchmark": "D102 09a synthetic study",
            "synthetic_only": True,
            "contracts": contract_count,
            "hourly_bars_per_contract": hourly_bars,
            "total_hourly_bars": contract_count * hourly_bars,
            "funding_interval_hours": 4,
            "database_build_elapsed_seconds": round(build_seconds, 3),
            "database_bytes_before_cleanup": db_bytes,
            "database_build_peak_process_tree_working_set_bytes": memory.peak_bytes,
            "runs": runs,
            "body_digest_equal": same_digest,
            "position_stream_bytes_and_digest_equal": same_positions,
            "temporary_database_removed": False,
        }
    result["temporary_database_removed"] = not (temporary_root / "synthetic.sqlite").exists()
    return result


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contracts", type=int, default=600)
    parser.add_argument("--hourly-bars", type=int, default=43_000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "docs" / "reports" / "D102-09a-synthetic-benchmark.json",
    )
    args = parser.parse_args()
    if args.contracts < 1 or args.hourly_bars < 1 or args.workers < 2:
        parser.error("contracts/bars must be positive and workers must be at least 2")
    report = run_benchmark(
        contract_count=args.contracts,
        hourly_bars=args.hourly_bars,
        workers=args.workers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
