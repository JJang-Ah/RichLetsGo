"""D97 metadata-only universe selection. No candle or funding endpoint is reachable."""

import argparse
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from richletsgo.backtest.codec import digest, encode
from richletsgo.domain.clock import require_utc

from .bybit_public import BybitPublicClient, RetryPolicy, Transport, UrllibTransport

ALLOWED = frozenset({"/v5/market/instruments-info", "/v5/market/tickers"})
EXCLUDED_BASES = frozenset(
    {
        "USDC",
        "USDE",
        "USDT",
        "FDUSD",
        "DAI",
        "TUSD",
        "PAXG",
        "XAUT",
        "USDP",
        "BUSD",
        "USD1",
        "PYUSD",
        "EUR",
        "EURC",
        "GBP",
        "JPY",
        "CHF",
        "AUD",
        "CAD",
        "BRL",
        "TRY",
        "RUB",
        "XAU",
        "GOLD",
    }
)
CUTOFF_MS = 1640995200000


class MetadataTransport:
    def __init__(self, inner: Transport) -> None:
        self.inner = inner

    def get(self, url: str, timeout: float) -> tuple[int, bytes]:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.bybit.com"
            or parsed.path not in ALLOWED
        ):
            raise ValueError("universe selection permits metadata endpoints only")
        return self.inner.get(url, timeout)


def select(
    instruments: list[dict[str, Any]], tickers: list[dict[str, Any]], captured_at: datetime
) -> dict[str, Any]:
    require_utc(captured_at)
    names = [r["symbol"] for r in instruments]
    if len(names) != len(set(names)):
        raise ValueError("duplicate instrument across pages")
    ticks = {r["symbol"]: r for r in tickers}
    if len(ticks) != len(tickers):
        raise ValueError("duplicate ticker")
    ranking = []
    for row in instruments:
        base = row["baseCoin"]
        if (
            row["contractType"] != "LinearPerpetual"
            or row["quoteCoin"] != "USDT"
            or row["status"] != "Trading"
            or int(row["launchTime"]) >= CUTOFF_MS
            or row["symbol"] in {"BTCUSDT", "ETHUSDT"}
            or base in EXCLUDED_BASES
            or not base
            or base[0].isdigit()
        ):
            continue
        turnover = Decimal(ticks[row["symbol"]]["turnover24h"])
        if not turnover.is_finite() or turnover < 0:
            raise ValueError("invalid turnover")
        ranking.append({"symbol": row["symbol"], "turnover24h": str(turnover)})
    ranking.sort(key=lambda r: (-Decimal(r["turnover24h"]), r["symbol"]))
    if len(ranking) < 4:
        raise ValueError("fewer than four eligible contracts; no substitution")
    # Preserve the exact metadata fields used, never expose price/funding fields in artifacts.
    fields = ("symbol", "baseCoin", "quoteCoin", "contractType", "status", "launchTime")
    body = {
        "rule_version": "D97-v1",
        "captured_at": captured_at.isoformat(),
        "rules": {
            "launch_before_ms": CUTOFF_MS,
            "excluded_bases": sorted(EXCLUDED_BASES),
            "excluded_symbols": ["BTCUSDT", "ETHUSDT"],
            "count": 4,
        },
        "instruments": sorted(
            [{k: r[k] for k in fields} for r in instruments], key=lambda r: r["symbol"]
        ),
        "tickers": sorted(
            [{k: r[k] for k in ("symbol", "turnover24h")} for r in tickers],
            key=lambda r: r["symbol"],
        ),
        "ranking": ranking,
        "selected": [r["symbol"] for r in ranking[:4]],
    }
    return {"body": body, "sha256": digest(body)}


def collect(client: BybitPublicClient, captured_at: datetime) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    cursor = ""
    seen = set()
    for _ in range(100):
        params = {"category": "linear", "status": "Trading", "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        result = client._get("/v5/market/instruments-info", params)
        rows.extend(result["list"])
        cursor = result.get("nextPageCursor", "")
        if not cursor:
            break
        if cursor in seen:
            raise ValueError("instrument cursor loop")
        seen.add(cursor)
    else:
        raise ValueError("instrument pagination incomplete")
    tickers = client._get("/v5/market/tickers", {"category": "linear"})["list"]
    return select(rows, tickers, captured_at)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Durable one-shot marker: a failed attempt must be audited, never silently rerun.
    with args.output.with_suffix(".started.json").open("x", encoding="utf-8") as stream:
        stream.write(encode({"started_at": datetime.now(UTC)}))
    if args.output.exists():
        raise ValueError("selection already exists")
    client = BybitPublicClient(
        "https://api.bybit.com",
        transport=MetadataTransport(UrllibTransport()),
        policy=RetryPolicy(0.2, 5, 1, 10),
        sleep=time.sleep,
    )
    result = collect(client, datetime.now(UTC))
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode(result))
    print(json.dumps({"selected": result["body"]["selected"], "sha256": result["sha256"]}))


if __name__ == "__main__":
    main()
