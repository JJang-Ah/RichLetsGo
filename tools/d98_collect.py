"""Collect D98 design-only data after implementation review and merge."""

import argparse
import subprocess
import time
import tomllib
from pathlib import Path

from richletsgo.marketdata.bybit_public import RetryPolicy, UrllibTransport
from richletsgo.marketdata.ingest import load_settings
from richletsgo.research.d98.boundaries import design_contracts, load_pool
from richletsgo.research.d98.collect import DesignClient, DesignTransport, collect
from richletsgo.research.d98.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Revisit pages to repair missing data before registration",
    )
    args = parser.parse_args()
    pool = load_pool(args.pool)
    allowed = set(design_contracts(pool))
    if args.symbols is not None and not set(args.symbols) <= allowed:
        raise ValueError("not a design symbol")
    # The published pool must be byte-identical to main before any network access.
    root = Path(__file__).resolve().parents[1]
    published = subprocess.check_output(
        ["git", "-C", str(root), "show", "origin/main:docs/reports/D98-pool.json"]
    )
    if published != args.pool.read_bytes():
        raise ValueError("pool is not the merged origin/main artifact; fetch first")
    directory, base = load_settings(args.config)
    settings = tomllib.loads(args.config.read_text(encoding="utf-8"))["d98"]["collection"]
    policy = RetryPolicy(
        settings["pause_seconds"],
        settings["max_attempts"],
        settings["backoff_seconds"],
        settings["timeout_seconds"],
    )
    client = DesignClient(
        base, transport=DesignTransport(UrllibTransport(), allowed), policy=policy, sleep=time.sleep
    )
    store = Store(args.db or directory / "d98-design.sqlite")
    try:
        collect(pool, store, client, symbols=args.symbols, refresh=args.refresh)
    finally:
        store.close()
    print("D98 design collection complete; run d98_audit.py before registration")


if __name__ == "__main__":
    main()
