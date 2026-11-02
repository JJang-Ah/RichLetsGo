"""Synthetic 600 x 43000 D98 storage/coverage/hash benchmark, no network."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from benchmark_backtest import peak_process_mib

from richletsgo.backtest.codec import digest, encode
from richletsgo.research.d98 import audit as audit_module
from richletsgo.research.d98.audit import contract_audit
from richletsgo.research.d98.boundaries import BTC_START, DAY, END, HOUR, START
from richletsgo.research.d98.store import Store


def measured_contract(args):
    result = contract_audit(args)
    Path(os.environ["D98_BENCH_METRICS"], str(os.getpid()) + ".json").write_text(
        json.dumps(peak_process_mib())
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contracts", type=int, default=600)
    parser.add_argument("--bars", type=int, default=43000)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    db = args.output / "synthetic.sqlite"
    cs = []
    for n in range(args.contracts):
        symbol = "BTCUSDT" if n == 0 else f"SYN{n}USDT"
        cs.append(
            dict(
                symbol=symbol,
                key=symbol,
                launch=START,
                delist=None,
                status="Trading",
                pool="DESIGN",
                excluded_reason=None,
                priority=[1, START, symbol],
            )
        )
    body = {
        "rule_version": "D98-pool-v1",
        "contracts": cs,
        "instruments": [{"symbol": c["symbol"], "fundingInterval": "480"} for c in cs],
    }
    pool = {"body": body, "sha256": digest(body)}
    store = Store(db)
    store.bind(pool["sha256"])
    begin = time.perf_counter()
    for n, c in enumerate(cs):
        with store.db:
            store.put(
                "bars",
                c["symbol"],
                (
                    [START + i * HOUR, "100", "101", "99", "100", "10", "1000"]
                    for i in range(args.bars)
                ),
            )
            store.put(
                "funding",
                c["symbol"],
                ([t, "0.0001"] for t in range(START - DAY, START + args.bars * HOUR, 8 * HOUR)),
            )
        if n % 50 == 49:
            print("stored", n + 1, flush=True)
    with store.db:
        store.put(
            "daily",
            "BTCUSDT",
            ([t, "100", "101", "99", "100", "10", "1000"] for t in range(BTC_START, END, DAY)),
        )
    store.close()
    result = {
        "synthetic_only": True,
        "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "contracts": args.contracts,
        "bars_per_contract": args.bars,
        "storage_seconds": time.perf_counter() - begin,
        "storage_peak_mib": peak_process_mib(),
        "runs": [],
    }
    audit_module.contract_audit = measured_contract
    previous = None
    for workers in (1, args.workers):
        metrics = args.output / f"memory-{workers}"
        metrics.mkdir()
        os.environ["D98_BENCH_METRICS"] = str(metrics.resolve())
        begin = time.perf_counter()
        report = audit_module.audit(pool, db, workers=workers)
        seconds = time.perf_counter() - begin
        peaks = {p.stem: json.loads(p.read_text()) for p in metrics.glob("*.json")}
        parent = peak_process_mib()
        child = sum(v for k, v in peaks.items() if k != str(os.getpid()))
        run = {
            "workers": workers,
            "seconds": seconds,
            "data_hash": report["body"]["data_hash"],
            "audit_digest": report["sha256"],
            "parent_peak_mib": parent,
            "worker_peaks_mib": peaks,
            "sum_peak_upper_bound_mib": parent + child,
        }
        result["runs"].append(run)
        if previous is not None:
            assert report == previous
        previous = report
        print(encode(run), flush=True)
    result["equal"] = True
    (args.output / "benchmark.json").write_text(encode(result) + "\n", encoding="utf-8")
    print("complete", flush=True)


if __name__ == "__main__":
    main()
