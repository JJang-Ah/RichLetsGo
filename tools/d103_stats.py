"""Summarize the D103 structure DB into a JSON artifact (read-only, no network)."""

import argparse
import json
from pathlib import Path

from richletsgo.backtest.codec import encode
from richletsgo.research.d103.allowlist import build, crypto_assets, load_snapshot
from richletsgo.research.d103.models import SNAPSHOT_DIR
from richletsgo.research.d103.stats import load_halts, summarize
from richletsgo.research.d103.store import Store

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    # Announced halts (splits etc.) compiled after collection; required before any stats
    # (M2-10 3). A file with an empty mapping still records when it was checked.
    parser.add_argument("--halts", type=Path, required=True)
    parser.add_argument("--sessions", type=Path, default=ROOT / "docs/reports/D103-sessions.json")
    args = parser.parse_args()
    pool = json.loads((ROOT / "docs/reports/D98-pool.json").read_text(encoding="utf-8"))["body"]
    allowlist = build(load_snapshot(ROOT / SNAPSHOT_DIR), crypto_assets(pool))
    halts = load_halts(json.loads(args.halts.read_text(encoding="utf-8")))
    store = Store(args.db, readonly=True)
    try:
        # Re-apply the held set recorded at collection and require the same allowlist digest.
        for (content,) in store.rows("SELECT content FROM log ORDER BY id"):
            event = json.loads(content)
            if "held" in event:
                allowlist.held = dict(event["held"])
        if allowlist.sha256() != store.get("allowlist_sha256"):
            raise ValueError("allowlist differs from the one bound at collection")
        result = {
            "allowlist_sha256": store.get("allowlist_sha256"),
            "T0": store.get("T0"),
            "data_hash": store.data_hash(),
            "summary": summarize(
                store,
                allowlist,
                json.loads(args.sessions.read_text(encoding="utf-8")),
                halts,
            ),
        }
    finally:
        store.close()
    args.output.write_text(encode(result) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
