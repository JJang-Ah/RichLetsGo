"""Benchmark D101 against a temporary 600-contract synthetic SQLite dataset."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import threading
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from richletsgo.backtest.codec import digest
from richletsgo.research.d98.boundaries import BTC_START, DAY, END, HOUR, START
from richletsgo.research.d98.models import RawBar
from richletsgo.research.d98.store import Store
from richletsgo.research.d101.models import D98_FREEZE_DIGEST
from richletsgo.research.d101.study import study


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def _process_tree_working_set() -> int:
    if os.name != "nt":
        raise RuntimeError("this benchmark's process-tree RSS sampler requires Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry)]
    kernel.Process32FirstW.restype = ctypes.c_int
    kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry)]
    kernel.Process32NextW.restype = ctypes.c_int
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_MemoryCounters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int

    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    parents: dict[int, int] = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                entry.dwSize = ctypes.sizeof(entry)
                if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel.CloseHandle(snapshot)

    root = os.getpid()
    process_ids = {root}
    changed = True
    while changed:
        before = len(process_ids)
        process_ids.update(pid for pid, parent in parents.items() if parent in process_ids)
        changed = len(process_ids) != before

    total = 0
    for pid in process_ids:
        handle = kernel.OpenProcess(0x0400, 0, pid)  # PROCESS_QUERY_INFORMATION
        if not handle:
            continue
        try:
            counters = _MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), ctypes.sizeof(counters)):
                total += int(counters.WorkingSetSize)
        finally:
            kernel.CloseHandle(handle)
    return total


class _PeakMemory:
    def __init__(self, interval: float = 0.2) -> None:
        self.interval = interval
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def __enter__(self) -> _PeakMemory:
        self._sample_once()
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._stop.set()
        self._thread.join()
        self._sample_once()

    def _sample_once(self) -> None:
        self.peak_bytes = max(self.peak_bytes, _process_tree_working_set())

    def _sample(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._sample_once()
            except OSError:
                continue


def _synthetic_pool(contracts: list[dict[str, Any]]) -> dict[str, Any]:
    body = {
        "rule_version": "D98-pool-v1",
        "contracts": contracts,
        "instruments": [
            {"symbol": contract["symbol"], "fundingInterval": "240"} for contract in contracts
        ],
    }
    return {"body": body, "sha256": digest(body)}


def _build_database(path: Path, contracts: list[dict[str, Any]], hours: int) -> None:
    data_end = START + hours * HOUR
    store = Store(path)
    try:
        store.bind(_synthetic_pool(contracts)["sha256"])
        for contract in contracts:
            symbol = contract["symbol"]
            with store.db:
                store.put(
                    "bars",
                    symbol,
                    (
                        [at, "100", "101", "99", "100", "1", "1000000"]
                        for at in range(START, data_end, HOUR)
                    ),
                )
                store.put(
                    "funding",
                    symbol,
                    ([at, "0"] for at in range(START - 8 * HOUR, data_end + 1, 4 * HOUR)),
                )
        with store.db:
            store.put(
                "daily",
                "BTCUSDT",
                (
                    [bar.start, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.turnover]
                    for bar in _btc_daily_rows()
                ),
            )
            store.freeze(D98_FREEZE_DIGEST)
    finally:
        store.close()


def _btc_daily_rows() -> Iterator[RawBar]:
    for at in range(BTC_START, END, DAY):
        yield RawBar(
            at,
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100"),
            Decimal("1"),
            Decimal("1000000"),
        )


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
        "data_hash": digest(["D101-synthetic-benchmark-v1", contract_count, hourly_bars]),
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
    temp_directory_name = "d101-synthetic-benchmark-"
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
                    "trades_sha256": result["body"]["trades"]["sha256"],
                    "trade_count": result["body"]["trades"]["count"],
                    "trade_bytes": result["body"]["trades"]["bytes"],
                }
            )
        same_digest = runs[0]["body_sha256"] == runs[1]["body_sha256"]
        same_trades = (
            runs[0]["trades_sha256"] == runs[1]["trades_sha256"]
            and (temporary_root / "output-1" / "D101-study-trades.jsonl").read_bytes()
            == (temporary_root / f"output-{workers}" / "D101-study-trades.jsonl").read_bytes()
        )
        if not same_digest or not same_trades:
            raise AssertionError("single-worker and multi-worker outputs differ")
        result = {
            "benchmark": "D101 08a synthetic study",
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
            "trade_stream_bytes_and_digest_equal": same_trades,
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
        default=root / "docs" / "reports" / "D101-08a-synthetic-benchmark.json",
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
