"""D97 two-campaign preparation barrier and immutable input-failure audit."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .campaign import InputUnavailable, atomic_json, campaign_lock, file_hash
from .codec import digest
from .walkforward import run_campaign
from .wf_config import load_walk_config


def paired(
    primary: Path, secondary: Path, commit: str, now: datetime, *, prepare_only: bool
) -> Path:
    a, b = load_walk_config(primary), load_walk_config(secondary)
    if (
        set(a.grids) != {"S1B"}
        or set(b.grids) != {"S1B-S2P"}
        or a.preregistration is None
        or a.preregistration != b.preregistration
        or a.research.symbols != b.research.symbols
        or a.start != b.start
        or a.end != b.end
        or a.warmup_start != b.warmup_start
        or a.windows != b.windows
        or a.research.market_data_dir != b.research.market_data_dir
        or a.research.results_dir != b.research.results_dir
    ):
        raise ValueError("paired campaigns must share registration, universe, windows and inputs")
    identity = {
        "preregistration": a.preregistration,
        "code_commit": commit,
        "configs": [file_hash(primary), file_hash(secondary)],
    }
    root = a.research.results_dir / "pairs" / digest(identity)
    with campaign_lock(root):
        audit = root / "pair.json"
        previous = json.loads(audit.read_text(encoding="utf-8")) if audit.exists() else None
        if previous and previous.get("sha256") != digest(previous["body"]):
            raise ValueError("paired audit digest mismatch")
        if previous and previous["body"]["status"] == "INVALID_INPUT":
            return root  # Failed registration is final; never silently recollect/retry.
        roots = []
        try:
            for path in (primary, secondary):
                roots.append(run_campaign(path, commit, now, prepare_only=True))
        except InputUnavailable as exc:
            # Snapshot hashing remains possible even when semantic fingerprinting cannot
            # pin a missing tier/instrument. No claim that this is a valid data_hash.
            database = a.research.market_data_dir / "market_data.sqlite3"
            import sqlite3
            from contextlib import closing

            frozen = root / "failed-input.sqlite3"
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                with closing(sqlite3.connect(frozen)) as snapshot:
                    db.backup(snapshot)
                    provenance = [
                        list(row)
                        for row in snapshot.execute("SELECT * FROM ingest_runs ORDER BY id")
                    ]
            files = {frozen.name: file_hash(frozen)}
            body: dict[str, Any] = {
                **identity,
                "status": "INVALID_INPUT",
                "primary": "INVALID_INPUT",
                "secondary": "INVALID_INPUT",
                "at": now.isoformat(),
                "symbol": exc.symbol.value,
                "item": exc.item,
                "exception": type(exc).__name__,
                "ingest_runs": provenance,
                "partial_input_files": files,
                "partial_fingerprint": digest(files),
                "note": "partial physical snapshot fingerprint; no valid campaign data_hash",
            }
            atomic_json(audit, {"body": body, "sha256": digest(body)})
            (root / "report.md").write_text(
                "# D97 INVALID_INPUT\n\nBoth judgments stopped before computation. "
                "No symbol or period substitution. A new decision and registration are required.\n\n"
                f"Failure: {exc.symbol.value} / {exc.item}\n\nAudit SHA-256: `{digest(body)}`\n",
                encoding="utf-8",
            )
            return root
        manifests = [json.loads((p / "manifest.json").read_text(encoding="utf-8")) for p in roots]
        if manifests[0]["identity"]["data_hash"] != manifests[1]["identity"]["data_hash"]:
            raise ValueError("paired data_hash mismatch")
        body = {
            **identity,
            "status": "PREPARED",
            "data_hash": manifests[0]["identity"]["data_hash"],
            "campaigns": [p.name for p in roots],
        }
        if previous and previous["body"] != body:
            raise ValueError("paired preparation changed")
        atomic_json(audit, {"body": body, "sha256": digest(body)})
        if not prepare_only and previous is None:
            raise ValueError(
                "publish both prepared manifests before computing; rerun same pair after publication"
            )
        if not prepare_only:
            for path in (primary, secondary):
                run_campaign(path, commit, now)
        return root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary", required=True, type=Path)
    parser.add_argument("--secondary", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    print(
        paired(
            args.primary,
            args.secondary,
            args.commit,
            datetime.now(UTC),
            prepare_only=args.prepare_only,
        )
    )


if __name__ == "__main__":
    main()
