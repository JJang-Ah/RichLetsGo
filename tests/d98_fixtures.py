"""Synthetic-only D98 study fixtures; never open the actual design database."""

from pathlib import Path
from typing import Any

from richletsgo.backtest.codec import digest
from richletsgo.research.d98.audit import audit
from richletsgo.research.d98.boundaries import BTC_START, DAY, END, HOUR, START, STUDY_START, epoch
from richletsgo.research.d98.models import D, RawBar
from richletsgo.research.d98.store import Store

T = STUDY_START + DAY


def bar(
    t: int,
    close: str = "100",
    *,
    opening: str | None = None,
    high: str = "105",
    low: str = "95",
    turnover: str = "1000000",
) -> RawBar:
    return RawBar(t, D(opening or close), D(high), D(low), D(close), D(10), D(turnover))


def daily_bars() -> list[RawBar]:
    result = []
    for t in range(BTC_START, END, DAY):
        if t < epoch("2021-01-01"):
            result.append(bar(t))
        elif t == epoch("2021-01-01"):
            result.append(bar(t, "110", high="115"))
        else:
            result.append(bar(t, "110", high="120", low="90"))
    return result


def contract(
    symbol: str = "SYNUSDT", *, end: int | None = T + 8 * HOUR, key: str = "SYN"
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "key": key,
        "launch": START,
        "delist": end,
        "status": "Closed" if end else "Trading",
        "pool": "DESIGN",
        "excluded_reason": None,
        "priority": [1, START, symbol],
    }


def make_pool(cs: list[dict[str, Any]]) -> dict[str, Any]:
    body = {
        "rule_version": "D98-pool-v1",
        "contracts": cs,
        "instruments": [{"symbol": c["symbol"], "fundingInterval": "60"} for c in cs],
    }
    return {"body": body, "sha256": digest(body)}


def raw(b: RawBar) -> list[Any]:
    return [b.start, b.open, b.high, b.low, b.close, b.volume, b.turnover]


def dataset(path: Path, *, end: int = T + 8 * HOUR) -> tuple[dict[str, Any], dict[str, Any]]:
    c = contract(end=end)
    pool = make_pool([c])
    store = Store(path)
    store.bind(pool["sha256"])
    rows = []
    for t in range(START, end, HOUR):
        if t >= T - 4 * HOUR:
            b = bar(t, "120", high="125", low="115", turnover="6000000" if t < T else "1000000")
        else:
            b = bar(t, turnover="6000000" if t >= T - DAY else "1000000")
        rows.append(raw(b))
    with store.db:
        store.put("bars", "SYNUSDT", rows)
        store.put("funding", "SYNUSDT", ([t, "0"] for t in range(START - DAY, end + 1, 4 * HOUR)))
        store.put("daily", "BTCUSDT", (raw(b) for b in daily_bars()))
    store.close()
    return pool, audit(pool, path)


def collected_dataset(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay synthetic response pages through the real 07b collector into 07c."""
    import json
    from urllib.parse import parse_qs, urlsplit

    from richletsgo.marketdata.bybit_public import RetryPolicy
    from richletsgo.research.d98.collect import DesignClient, DesignTransport, collect

    source = path.with_name("synthetic-response-source.sqlite")
    pool, _ = dataset(source)
    pool = make_pool([*pool["body"]["contracts"], contract("BTCUSDT", end=START, key="BTC")])
    read = Store(source, readonly=True)
    data = {
        (kind, symbol): list(read.rows(kind, symbol))
        for kind, symbol in [("bars", "SYNUSDT"), ("funding", "SYNUSDT"), ("daily", "BTCUSDT")]
    }
    read.close()

    class Transport:
        def get(self, url, timeout):
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            symbol = q["symbol"]
            kind = "funding" if "funding" in url else "daily" if q["interval"] == "D" else "bars"
            first, last = (
                (int(q["startTime"]), int(q["endTime"]))
                if kind == "funding"
                else (int(q["start"]), int(q["end"]))
            )
            selected = [r for r in data[kind, symbol] if first <= r[0] <= last]
            rows = (
                [
                    dict(symbol=symbol, fundingRateTimestamp=str(r[0]), fundingRate=r[1])
                    for r in selected
                ]
                if kind == "funding"
                else selected
            )
            return 200, json.dumps(
                dict(retCode=0, result=dict(symbol=symbol, category="linear", list=rows[::-1]))
            ).encode()

    client = DesignClient(
        "https://api.bybit.com",
        transport=DesignTransport(Transport(), {"SYNUSDT", "BTCUSDT"}),
        policy=RetryPolicy(0, 1, 0, 1),
        sleep=lambda _: None,
    )
    store = Store(path)
    try:
        collect(pool, store, client)
    finally:
        store.close()
    return pool, audit(pool, path)
