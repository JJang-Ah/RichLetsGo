"""Symbol allowlist from the preserved 2026-11-10 metadata snapshot (M2-10 section 1)."""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from richletsgo.backtest.codec import digest

from .models import SNAPSHOT_FILES, TYPES

CRYPTO_TYPES = frozenset({"", "innovation"})
IDENTITY = ("symbolType", "status", "symbolId", "contractType", "settleCoin", "quoteCoin")


@dataclass(frozen=True)
class Entry:
    symbol: str
    symbol_type: str
    base: str
    launch: int
    region: str
    ticker: str
    symbol_id: str
    upper_funding: str
    identity: tuple[str, ...]


@dataclass
class Allowlist:
    entries: dict[str, Entry]
    rejected: dict[str, str] = field(default_factory=dict)
    held: dict[str, str] = field(default_factory=dict)

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(s for s in self.entries if s not in self.held)

    def sha256(self) -> str:
        return digest(
            {
                "symbols": sorted(self.symbols),
                "rejected": self.rejected,
                "held": self.held,
            }
        )


def load_snapshot(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """Verify SHA256SUMS before parsing; the snapshot replaces any re-download."""
    sums: dict[str, str] = {}
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        value, name = line.split(maxsplit=1)
        sums[name.lstrip("*")] = value
    if set(sums) != set(SNAPSHOT_FILES.values()):
        raise ValueError("snapshot file set mismatch")
    result: dict[str, list[dict[str, Any]]] = {}
    for symbol_type, name in SNAPSHOT_FILES.items():
        raw = (directory / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sums[name]:
            raise ValueError(f"snapshot hash mismatch: {name}")
        payload = json.loads(raw)
        body = payload["result"]
        if payload.get("retCode") != 0 or body.get("category") != "linear":
            raise ValueError(f"snapshot response invalid: {name}")
        if body.get("nextPageCursor"):
            raise ValueError(f"snapshot is not a single complete page: {name}")
        result[symbol_type] = body["list"]
    return result


def crypto_assets(pool_body: dict[str, Any]) -> tuple[frozenset[str], frozenset[str]]:
    """Symbols and base coins of every D98 crypto instrument (design and judgment)."""
    rows = [i for i in pool_body["instruments"] if i["symbolType"] in CRYPTO_TYPES]
    return frozenset(r["symbol"] for r in rows), frozenset(r["baseCoin"] for r in rows)


def _identity(row: dict[str, Any]) -> tuple[str, ...]:
    return (*(str(row.get(k, "")) for k in IDENTITY), str(row.get("isPreListing")))


def build(
    snapshot: dict[str, list[dict[str, Any]]],
    crypto: tuple[frozenset[str], frozenset[str]],
) -> Allowlist:
    symbols, bases = crypto
    allowlist = Allowlist({})
    for requested, rows in snapshot.items():
        for row in rows:
            symbol = row["symbol"]
            kind = row.get("symbolType")
            if kind not in TYPES:
                raise ValueError(f"unknown symbolType {kind!r} for {symbol}")
            if kind != requested:
                raise ValueError(f"symbolType differs from request for {symbol}")
            if symbol in allowlist.entries or symbol in allowlist.rejected:
                raise ValueError(f"duplicate symbol {symbol}")
            if symbol in symbols or row.get("baseCoin") in bases:
                allowlist.rejected[symbol] = "d98_crypto_overlap"
                continue
            if (
                row.get("contractType") != "LinearPerpetual"
                or row.get("settleCoin") != "USDT"
                or row.get("quoteCoin") != "USDT"
                or row.get("status") != "Trading"
                or row.get("isPreListing") is not False
            ):
                allowlist.rejected[symbol] = "not_included"
                continue
            allowlist.entries[symbol] = Entry(
                symbol,
                kind,
                str(row["baseCoin"]),
                int(row["launchTime"]),
                str(row.get("marketRegion", "")),
                str(row.get("underlyingTicker", "")),
                str(row.get("symbolId", "")),
                str(row.get("upperFundingRate", "")),
                _identity(row),
            )
    return allowlist


def hold_changed(allowlist: Allowlist, fresh: dict[str, list[dict[str, Any]]]) -> None:
    """Fresh metadata only detects change: changed or missing symbols are held, new ignored."""
    current = {row["symbol"]: row for rows in fresh.values() for row in rows}
    for symbol, entry in allowlist.entries.items():
        row = current.get(symbol)
        if row is None:
            allowlist.held[symbol] = "missing_now"
        elif _identity(row) != entry.identity:
            allowlist.held[symbol] = "identity_changed"
