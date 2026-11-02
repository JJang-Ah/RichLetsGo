"""Pure D98 pool construction and an instruments-only transport boundary."""

import hashlib
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, urlsplit

from richletsgo.backtest.codec import digest
from richletsgo.domain.clock import require_utc
from richletsgo.marketdata.bybit_public import BybitPublicClient, Transport

from . import tables

VERSION = "D98-pool-v1"
ENDPOINT = "/v5/market/instruments-info"
FIELDS = (
    "symbol",
    "baseCoin",
    "quoteCoin",
    "settleCoin",
    "contractType",
    "status",
    "launchTime",
    "deliveryTime",
    "fundingInterval",
    "symbolType",
    "fullName",
)


class MetadataTransport:
    def __init__(self, inner: Transport) -> None:
        self.inner = inner

    def get(self, url: str, timeout: float) -> tuple[int, bytes]:
        p = urlsplit(url)
        q = parse_qs(p.query, keep_blank_values=True)
        if (
            p.scheme != "https"
            or p.netloc != "api.bybit.com"
            or p.path != ENDPOINT
            or p.fragment
            or set(q) - {"category", "status", "limit", "cursor"}
            or q.get("category") != ["linear"]
            or q.get("status") not in (["Trading"], ["Closed"])
            or q.get("limit") != ["1000"]
            or any(len(v) != 1 for v in q.values())
        ):
            raise ValueError("D98 permits only linear Trading/Closed instruments-info pages")
        return self.inner.get(url, timeout)


def collect(client: BybitPublicClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for status in ("Trading", "Closed"):
        cursor = ""
        seen: set[str] = set()
        for _ in range(1000):
            result = client.instruments_page(status, cursor=cursor)
            page = result["list"]
            if not isinstance(page, list) or any(not isinstance(r, dict) for r in page):
                raise ValueError("unexpected instruments page")
            rows.extend(page)
            cursor = result.get("nextPageCursor", "")
            if not isinstance(cursor, str):
                raise ValueError("invalid cursor")
            if not cursor:
                break
            if cursor in seen:
                raise ValueError("instrument cursor loop")
            seen.add(cursor)
        else:
            raise ValueError("incomplete instrument pagination")
    return normalize(rows)


def normalize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen: set[str] = set()
    for row in rows:
        item = {k: row[k] for k in FIELDS}
        if any(not isinstance(item[k], str) for k in (*FIELDS[:6], "symbolType", "fullName")):
            raise ValueError("instrument fields must be strings")
        if item["symbol"] in seen:
            raise ValueError("duplicate instrument")
        seen.add(item["symbol"])
        for k in ("launchTime", "deliveryTime", "fundingInterval"):
            if isinstance(item[k], bool) or not re.fullmatch(r"[0-9]+", str(item[k])):
                raise ValueError(f"invalid {k}")
            item[k] = str(int(item[k]))
        result.append(item)
    return sorted(result, key=lambda r: r["symbol"])


def asset_key(base: str, exceptions: frozenset[str] | None = None) -> tuple[str, int]:
    name = unicodedata.normalize("NFKC", base).upper()
    if not name:
        raise ValueError("empty baseCoin")
    exceptions = tables.NUMERIC_EXCEPTIONS if exceptions is None else exceptions
    multiplier = 1
    if name not in exceptions:
        # A whole numeric name is preserved; never fall through to a shorter prefix.
        powers = [str(10**n) for n in range(7, 0, -1)]
        for prefix in (True, False):
            found = False
            for power in powers:
                if name.startswith(power) if prefix else name.endswith(power):
                    if len(name) > len(power):
                        name = name[len(power) :] if prefix else name[: -len(power)]
                        multiplier = int(power)
                    found = True
                    break
            if found:
                break
    return tables.ALIASES.get(name, name), multiplier


def exclusion(row: dict[str, Any]) -> str | None:
    symbol_type = row["symbolType"]
    if symbol_type in {"stock", "ETF", "commodity", "forex"}:
        return f"non_crypto:{symbol_type}"
    if symbol_type not in {"", "innovation"}:
        raise ValueError(f"unknown symbolType: {symbol_type}")
    for field, expected in (
        ("contractType", "LinearPerpetual"),
        ("quoteCoin", "USDT"),
        ("settleCoin", "USDT"),
    ):
        if row[field] != expected:
            return field
    if row["status"] not in {"Trading", "Closed"}:
        return "status"
    if row["status"] == "Closed" and int(row["deliveryTime"]) == 0:
        return "unknown_delist"
    if not re.fullmatch(r"[0-9A-Z]+USDT", row["symbol"]):
        raise ValueError("unsupported symbol spelling; review contract before proceeding")
    if row["status"] == "Closed" and int(row["deliveryTime"]) <= int(row["launchTime"]):
        raise ValueError("delist must follow launch")
    return None


def review(rows: list[dict[str, Any]], captured_at: datetime, commit: str) -> dict[str, Any]:
    require_utc(captured_at)
    rows = normalize(rows)
    eligible = [r for r in rows if exclusion(r) is None]
    groups: dict[str, list[str]] = defaultdict(list)
    for r in eligible:
        key, _ = asset_key(r["baseCoin"])
        groups[key].append(r["symbol"])
    similar = []
    closed = [r for r in eligible if r["status"] == "Closed"]
    trading = [r for r in eligible if r["status"] == "Trading"]
    for a in closed:
        for b in trading:
            if SequenceMatcher(None, a["baseCoin"], b["baseCoin"]).ratio() >= 0.7:
                similar.append([a["symbol"], b["symbol"]])
    # No assignment call, assignment fields, or assignment counts in review mode.
    return {
        "mode": "metadata-review",
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "code_commit": commit,
        "instruments": rows,
        "input_sha256": digest(rows),
        "numeric_names": [
            r["symbol"]
            for r in eligible
            if unicodedata.normalize("NFKC", r["baseCoin"])[0].isdigit()
        ],
        "same_key_groups": {k: v for k, v in sorted(groups.items()) if len(v) > 1},
        "similar_closed_trading": similar,
    }


def assignment(key: str) -> str:
    if key in tables.OBSERVED:
        return "DESIGN"
    return (
        "DESIGN" if hashlib.sha256(f"{VERSION}|{key}".encode()).digest()[0] % 2 == 0 else "JUDGMENT"
    )


def build(rows: list[dict[str, Any]], captured_at: datetime, commit: str) -> dict[str, Any]:
    require_utc(captured_at)
    rows = normalize(rows)
    contracts = []
    keys: dict[str, dict[str, Any]] = {}
    for row in rows:
        reason = exclusion(row)
        key, multiplier = asset_key(row["baseCoin"])
        if reason is None and key in tables.EXCLUDED:
            reason = "excluded_asset"
        pool = None if reason else assignment(key)
        launch = int(row["launchTime"])
        contract = {
            "symbol": row["symbol"],
            "status": row["status"],
            "launch": launch,
            "delist": int(row["deliveryTime"]) if row["status"] == "Closed" else None,
            "key": key,
            "multiplier": multiplier,
            "pool": pool,
            "excluded_reason": reason,
            "priority": [multiplier, launch, row["symbol"]],
        }
        contracts.append(contract)
        if pool:
            keys.setdefault(key, {"pool": pool, "contracts": []})["contracts"].append(row["symbol"])
    counts = {
        "excluded": sum(c["excluded_reason"] is not None for c in contracts),
        "closed": sum(c["status"] == "Closed" for c in contracts),
    }
    for label in ("DESIGN", "JUDGMENT"):
        counts[label + "_contracts"] = sum(c["pool"] == label for c in contracts)
        counts[label + "_keys"] = sum(k["pool"] == label for k in keys.values())
    body = {
        "rule_version": VERSION,
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "code_commit": commit,
        "instruments": rows,
        "input_sha256": digest(rows),
        "tables": tables.snapshot(),
        "contracts": contracts,
        "keys": dict(sorted(keys.items())),
        "counts": counts,
    }
    return {"body": body, "sha256": digest(body)}


def active_contract(contracts: list[dict[str, Any]], key: str, at_ms: int) -> str | None:
    active = [
        c
        for c in contracts
        if c["key"] == key
        and c["excluded_reason"] is None
        and c["launch"] <= at_ms
        and (c["delist"] is None or at_ms < c["delist"])
    ]
    return min(active, key=lambda c: c["priority"])["symbol"] if active else None


def markdown(artifact: dict[str, Any]) -> str:
    body = artifact["body"]
    lines = ["# D98 pool", "", f"Digest: `{artifact['sha256']}`", "", "## Counts", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(body["counts"].items())]
    lines += ["", "## Frozen tables", ""]
    lines += [f"- {k}: {v}" for k, v in body["tables"].items()]
    return "\n".join(lines) + "\n"
