"""Verify a published registration artifact before accessing new-symbol inputs.

The envelope's digest is of `body`, which excludes its own digest. Publication is
a human review step; this guard detects missing/changed artifacts, not publication.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from richletsgo.domain import Symbol

from .codec import digest

LEGACY_SYMBOLS = (Symbol.BTCUSDT, Symbol.ETHUSDT)


def load_registration(path: Path) -> tuple[dict[str, Any], str]:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    body, sha = envelope["body"], envelope["sha256"]
    if not isinstance(body, dict) or digest(body) != sha or not body.get("id"):
        raise ValueError("preregistration digest/id mismatch")
    return body, sha


def check_collection(
    path: Path | None, symbols: tuple[Symbol, ...], start: datetime, end: datetime
) -> None:
    if set(symbols) <= set(LEGACY_SYMBOLS):
        return
    if path is None:
        raise ValueError("new symbols require preregistration before data access")
    body, _ = load_registration(path)
    collection = body["collection"]
    if (
        not set(s.value for s in symbols) <= set(body["symbols"])
        or start < datetime.fromisoformat(collection["start"])
        or end > datetime.fromisoformat(collection["end_exclusive"])
    ):
        raise ValueError("collection is outside preregistered universe/range")
