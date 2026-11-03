"""Temporary synthetic-only D98 study benchmark; never takes an existing DB path."""

import argparse
import importlib
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmark_backtest import peak_process_mib

from richletsgo.backtest.codec import digest, encode
from richletsgo.research.d98.audit import audit
from richletsgo.research.d98.boundaries import BTC_START, DAY, END, HOUR, START, epoch
from richletsgo.research.d98.execution import atomic_text
from richletsgo.research.d98.store import Store
from richletsgo.research.d98.study import contract_study

study_module = importlib.import_module("richletsgo.research.d98.study")


def measured_contract(args):
    result = contract_study(args)
    Path(os.environ["D98_STUDY_METRICS"], str(os.getpid()) + ".json").write_text(
        json.dumps(peak_process_mib()), encoding="utf-8", newline="\n"
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contracts", type=int, default=600)
    parser.add_argument("--bars", type=int, default=43000)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.contracts < 1 or args.bars < 1500 or START + args.bars * HOUR > END or args.workers < 2:
        parser.error("positive contracts, 1500..collection-range bars, workers >= 2 required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        parser.error("output must be new")
    summary = dict(
        synthetic_only=True,
        contracts=args.contracts,
        bars_per_contract=args.bars,
        workload="Every 720h: 24h 6x turnover and final 4h price breakout; nonzero trades; actual indicators, 8 combinations.",
        runs=[],
    )
    with TemporaryDirectory(prefix="d98-study-benchmark-") as temporary:
        temp = Path(temporary)
        db = temp / "synthetic.sqlite"
        end = START + args.bars * HOUR
        contracts = [
            dict(
                symbol=f"SYN{n:04}USDT",
                key=f"SYN{n:04}",
                launch=START,
                delist=end,
                status="Closed",
                pool="DESIGN",
                excluded_reason=None,
                priority=[1, START, f"SYN{n:04}USDT"],
            )
            for n in range(args.contracts)
        ]
        body = dict(
            rule_version="D98-pool-v1",
            contracts=contracts,
            instruments=[dict(symbol=c["symbol"], fundingInterval="240") for c in contracts],
        )
        pool = dict(body=body, sha256=digest(body))
        store = Store(db)
        store.bind(pool["sha256"])
        begin = time.perf_counter()

        def rows():
            for i in range(args.bars):
                phase = i % 720
                price, high, low = ("120", "125", "115") if phase >= 716 else ("100", "105", "95")
                yield [
                    START + i * HOUR,
                    price,
                    high,
                    low,
                    price,
                    "10",
                    "6000000" if phase >= 696 else "1000000",
                ]

        for n, c in enumerate(contracts):
            with store.db:
                store.put("bars", c["symbol"], rows())
                store.put(
                    "funding",
                    c["symbol"],
                    ([t, "0.00001"] for t in range(START - DAY, end + 1, 4 * HOUR)),
                )
            if (n + 1) % 50 == 0:
                print("stored", n + 1, flush=True)
        with store.db:
            store.put(
                "daily",
                "BTCUSDT",
                (
                    [
                        t,
                        "110" if t >= epoch("2021-01-01") else "100",
                        "120"
                        if t > epoch("2021-01-01")
                        else "115"
                        if t == epoch("2021-01-01")
                        else "105",
                        "90",
                        "110" if t >= epoch("2021-01-01") else "100",
                        "10",
                        "1000000",
                    ]
                    for t in range(BTC_START, END, DAY)
                ),
            )
        store.close()
        summary["storage_seconds"] = time.perf_counter() - begin
        summary["storage_peak_mib"] = peak_process_mib()
        print("storage", summary["storage_seconds"], flush=True)
        begin = time.perf_counter()
        audited = audit(pool, db, workers=args.workers)
        summary["audit_seconds"] = time.perf_counter() - begin
        summary["data_hash"] = audited["body"]["data_hash"]
        assert audited["body"]["status"] == "READY"
        print("audited", summary["audit_seconds"], flush=True)
        study_module.contract_study = measured_contract
        previous = None
        for workers in (1, args.workers):
            metrics = temp / f"metrics-{workers}"
            metrics.mkdir()
            os.environ["D98_STUDY_METRICS"] = str(metrics)
            begin = time.perf_counter()
            result = study_module.study(
                pool,
                audited,
                db,
                temp / f"run-{workers}",
                workers=workers,
                registration_digest="synthetic-benchmark-only",
            )
            peaks = {p.stem: json.loads(p.read_text()) for p in metrics.glob("*.json")}
            parent = peak_process_mib()
            run = dict(
                workers=workers,
                seconds=time.perf_counter() - begin,
                parent_peak_mib=parent,
                worker_peaks_mib=peaks,
                sum_peak_upper_bound_mib=parent
                + sum(v for k, v in peaks.items() if k != str(os.getpid())),
                result_digest=result["sha256"],
                trades=result["body"]["trades"],
                stages=result["body"]["stages"],
            )
            assert result["body"]["trades"]["count"] > 0
            if previous is not None:
                assert result == previous
            previous = result
            summary["runs"].append(run)
            print(encode(run), flush=True)
    summary["temporary_database_removed"] = not db.exists()
    summary["equal"] = True
    assert summary["temporary_database_removed"]
    atomic_text(args.output, encode(summary) + "\n")
    print("complete", flush=True)


if __name__ == "__main__":
    main()
