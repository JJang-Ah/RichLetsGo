"""Offline three-year synthetic load benchmark (not trading performance).

Usage: python tools/benchmark_backtest.py --config config.example.toml --output <directory>
All data is generated inside the provided directory. Existing result hashes are immutable.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from richletsgo.backtest.config import load_research_config
from richletsgo.backtest.runner import BacktestRun
from richletsgo.domain import Symbol
from richletsgo.marketdata.models import (
    Bar,
    FundingRate,
    InstrumentSnapshot,
    Interval,
    RiskLimitTier,
)
from richletsgo.marketdata.store import MarketDataStore
from richletsgo.strategy.models import S1AParams, S1BParams, S2AParams, S2PParams


def peak_process_mib() -> float:
    """OS high-water working set, including data preparation and interpreter."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in ("peak", "working", "pp", "p", "np", "n", "page", "peak_page")
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.peak / 1024 / 1024
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--redesign", action="store_true", help="D95 synthetic 12-month load")
    args = parser.parse_args()
    config = load_research_config(args.config)
    start = datetime(2023, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)
    if args.redesign:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2027, 1, 1, tzinfo=UTC)
    first = start - timedelta(days=56 if args.redesign else 20)
    hour = timedelta(hours=1)
    with MarketDataStore(args.output / "synthetic-market") as store:
        for symbol in Symbol:
            store.put_instrument(
                InstrumentSnapshot(
                    symbol, D(".001"), D(".001"), D(".01"), D(100), timedelta(hours=8), end
                )
            )
            store.put_risk_tiers(
                [RiskLimitTier(symbol, 1, D(1000000), D(".005"), D(".01"), D(100), end)]
            )
            bars = []
            days = []
            for index in range((end - first) // hour):
                at = first + index * hour
                phase = (index // 24) % 60
                value = D(10000 + 10 * (phase if phase < 30 else 60 - phase))
                value += D(index % 3 - 1)
                bars.append(
                    Bar(symbol, Interval.HOUR, at, value, value + 2, value - 2, value, D(1), value)
                )
                if index % 24 == 23:
                    segment = bars[-24:]
                    days.append(
                        Bar(
                            symbol,
                            Interval.DAY,
                            segment[0].start,
                            segment[0].open,
                            max(b.high for b in segment),
                            min(b.low for b in segment),
                            segment[-1].close,
                            D(24),
                            sum((b.turnover for b in segment), D(0)),
                        )
                    )
            assert store.put_bars(bars).ok and store.put_bars(days).ok
            assert store.put_funding(
                [
                    FundingRate(symbol, start + i * hour, D(".0001"))
                    for i in range(-8, (end - start) // hour, 8)
                ]
            ).ok
        run = BacktestRun(
            start,
            end,
            S1BParams(55, 20) if args.redesign else S1AParams(5, 20, 10),
            S2PParams(2, D(20), 14, D("1.5"), D(2), 30)
            if args.redesign
            else S2AParams(20, D(2), 14, D(2), 24),
            config.profile,
            config.close_policy,
            config.sync_policy,
            tuple(Symbol),
            end,
            "synthetic-benchmark",
        )
        started = time.perf_counter()
        result = asyncio.run(
            run.execute(
                store, results_dir=args.output / "results", journal_dir=args.output / "journal"
            )
        )
        elapsed = time.perf_counter() - started
        report = {
            "kind": "synthetic_redesign_12m"
            if args.redesign
            else "synthetic_three_year_load_not_market_performance",
            "hours": (end - start) // hour,
            "elapsed_seconds": round(elapsed, 3),
            "peak_process_working_set_mib": round(peak_process_mib(), 2),
            "status": result.status,
            "trades": len(result.trades),
            "orders": len(result.orders),
            "result_hash": result.result_hash,
        }
        (args.output / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
