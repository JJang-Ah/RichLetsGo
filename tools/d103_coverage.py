"""Live-sample coverage of the D103 DB (read-only, no network, no price columns)."""

import argparse
import json
from pathlib import Path

from richletsgo.backtest.codec import encode
from richletsgo.research.d103.allowlist import build, crypto_assets, load_snapshot
from richletsgo.research.d103.coverage import bound_allowlist, coverage, restrict
from richletsgo.research.d103.models import SNAPSHOT_DIR
from richletsgo.research.d103.store import Store

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Expected symbols come from the preserved snapshot plus the held set logged at
    # collection, checked against the bound digest; no fresh metadata request.
    pool = json.loads((ROOT / "docs/reports/D98-pool.json").read_text(encoding="utf-8"))["body"]
    allowlist = build(load_snapshot(ROOT / SNAPSHOT_DIR), crypto_assets(pool))
    store = Store(args.db, readonly=True)
    try:
        restrict(store.db)
        expected = bound_allowlist(store, allowlist).symbols
        result = {"allowlist_sha256": store.get("allowlist_sha256"), **coverage(store, expected)}
    finally:
        store.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encode(result) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
