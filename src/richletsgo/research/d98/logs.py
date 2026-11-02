"""Bounded log summary and optional lossless, verifiable compressed export."""

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

from .store import Store


def summarize_log(store: Store, export: Path | None = None) -> dict[str, Any]:
    # Hash the uncompressed UTF-8 content column, in id order, with one LF per row.
    # Export exactly these bytes; gzip header differences do not affect identity.
    summary: dict[str, Any] = dict(
        rows=0,
        pages=0,
        requests=0,
        retried_pages=0,
        retries=0,
        error_pages=0,
        errors=[],
        refresh_pages=0,
        skipped_empty_symbols=[],
        exceptional_rows=[],
        collection_runs=[],
    )
    skipped: set[str] = set()
    h = hashlib.sha256()
    stream = gzip.open(export, "xb") if export is not None else None
    try:
        for (content,) in store.db.execute("SELECT content FROM collection_log ORDER BY id"):
            raw = (content + "\n").encode("utf-8")
            h.update(raw)
            if stream is not None:
                stream.write(raw)
            row = json.loads(content)
            summary["rows"] += 1
            if row.get("event") == "collection_start":
                summary["collection_runs"].append(row)
            if row.get("skip") == "empty lifetime":
                skipped.add(row["symbol"])
            if "page" not in row:
                continue
            attempts = row["attempts"]
            retries = sum(a["attempt"] > 1 for a in attempts)
            summary["pages"] += 1  # page executions, including refresh/failed executions
            summary["requests"] += len(attempts)
            summary["retried_pages"] += bool(retries)
            summary["retries"] += retries
            summary["refresh_pages"] += bool(row["refresh"])
            summary["error_pages"] += row["error"] is not None
            if row["error"] is not None:
                summary["errors"].append({"page": row["page"], "error": row["error"]})
            if retries or row["refresh"] or row["error"] is not None:
                summary["exceptional_rows"].append(row)
    finally:
        if stream is not None:
            stream.close()
    summary["skipped_empty_symbols"] = sorted(skipped)
    summary["sha256"] = h.hexdigest()
    summary["hash_format"] = (
        "collection_log.content in id order; UTF-8; LF after every row; uncompressed"
    )
    return summary
