"""D103 stage-0 collection: `history` (< 2026-09-01) or `live` (14-day orderbook samples)."""

import argparse
import json
import subprocess
import time
from pathlib import Path

from richletsgo.marketdata.bybit_public import RetryPolicy, UrllibTransport
from richletsgo.research.d103.allowlist import build, crypto_assets, hold_changed, load_snapshot
from richletsgo.research.d103.collect import collect_history, collect_live
from richletsgo.research.d103.guard import StructureClient, StructureTransport
from richletsgo.research.d103.models import SNAPSHOT_DIR, TYPES
from richletsgo.research.d103.store import Store

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.bybit.com"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("history", "live"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--sessions", type=Path, default=ROOT / "docs/reports/D103-sessions.json")
    args = parser.parse_args()
    # M2-10: at most 5 requests per second, 3 retries before a page is logged missing.
    policy = RetryPolicy(
        pause_seconds=0.25, max_attempts=4, backoff_seconds=1.0, timeout_seconds=10.0
    )
    pool = json.loads((ROOT / "docs/reports/D98-pool.json").read_text(encoding="utf-8"))["body"]
    allowlist = build(load_snapshot(ROOT / SNAPSHOT_DIR), crypto_assets(pool))
    launches = {s: e.launch for s, e in allowlist.entries.items()}
    client = StructureClient(
        BASE,
        transport=StructureTransport(UrllibTransport(), launches),
        policy=policy,
        sleep=time.sleep,
    )
    # Fresh metadata only detects change before any market request (M2-10 1).
    hold_changed(allowlist, {t: client.instruments(t)["list"] for t in TYPES})
    store = Store(args.db)
    try:
        if args.mode == "history":
            table = json.loads(args.sessions.read_text(encoding="utf-8"))
            commit = subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
            ).strip()
            collect_history(store, client, allowlist, table, code_commit=commit)
        else:
            collect_live(
                store,
                client,
                allowlist,
                clock=lambda: int(time.time() * 1000),
                sleep=time.sleep,
            )
    finally:
        store.close()
    print(f"D103 {args.mode} collection complete")


if __name__ == "__main__":
    main()
