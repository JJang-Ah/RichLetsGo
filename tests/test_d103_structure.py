"""D103 stage-0 collection guards and structure statistics on synthetic data only.

Synthetic symbols use an XQ prefix so they never name a real (or D98 judgment) contract.
"""

import json
import shutil
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from richletsgo.marketdata.bybit_public import RetryPolicy
from richletsgo.research.d103 import stats
from richletsgo.research.d103.allowlist import (
    Allowlist,
    Entry,
    build,
    crypto_assets,
    hold_changed,
    load_snapshot,
)
from richletsgo.research.d103.collect import collect_history, collect_live, history_tasks
from richletsgo.research.d103.guard import ObservationIncident, StructureClient, StructureTransport
from richletsgo.research.d103.models import BOUNDARY, DAY, HOUR, MINUTE, SNAPSHOT_DIR
from richletsgo.research.d103.sessions import Boundary, Sessions, us_extended
from richletsgo.research.d103.store import Store

ROOT = Path(__file__).resolve().parents[1]
POLICY = RetryPolicy(0, 1, 0, 1)
OLD = BOUNDARY - 3 * DAY  # launched before B
NEW = BOUNDARY + DAY  # launched after B: live only
API = "https://api.bybit.com"


def entry(symbol: str, launch: int) -> Entry:
    return Entry(symbol, "stock", symbol[:-4], launch, "US", symbol[:-4], "1", "0.02", ())


def allowlist() -> Allowlist:
    return Allowlist({"XQAUSDT": entry("XQAUSDT", OLD), "XQBUSDT": entry("XQBUSDT", NEW)})


def launches() -> dict[str, int]:
    return {"XQAUSDT": OLD, "XQBUSDT": NEW}


class FakeTransport:
    """Serves synthetic rows; records every URL that passed the guard."""

    def __init__(self, *, leak: bool = False, book_extra: bool = False) -> None:
        self.urls: list[str] = []
        self.leak = leak
        self.book_extra = book_extra

    def get(self, url: str, timeout: float) -> tuple[int, bytes]:
        self.urls.append(url)
        u = urlsplit(url)
        p = dict(parse_qsl(u.query))
        result: dict[str, object]
        if u.path.endswith("kline"):
            width = HOUR if p["interval"] == "60" else 5 * MINUTE
            start, end = int(p["start"]), int(p["end"])
            starts = list(range(start // width * width, end + 1, width))
            if self.leak:
                starts.append(BOUNDARY)
            if u.path == "/v5/market/kline":
                rows = [[str(s), "10", "11", "9", "10", "5", "50"] for s in starts]
            else:
                rows = [[str(s), "10", "10", "10", "10"] for s in starts]
            result = {"symbol": p["symbol"], "list": rows[::-1]}
        elif u.path == "/v5/market/funding/history":
            first, last = int(p["startTime"]), int(p["endTime"])
            times = [t for t in range(-(-first // (8 * HOUR)) * 8 * HOUR, last + 1, 8 * HOUR)]
            if self.leak:
                times.append(BOUNDARY)
            result = {
                "list": [
                    {"symbol": p["symbol"], "fundingRateTimestamp": str(t), "fundingRate": "0.0001"}
                    for t in times
                ]
            }
        elif u.path == "/v5/market/orderbook":
            result = {"s": p["symbol"], "b": [["9.9", "100"]], "a": [["10.1", "100"]], "ts": 1}
            if self.book_extra:
                result["price24hPcnt"] = "0.1"
        else:
            result = {"list": []}
        return 200, json.dumps({"retCode": 0, "result": result}).encode()


def client(transport: FakeTransport) -> StructureClient:
    return StructureClient(
        API,
        transport=StructureTransport(transport, launches()),
        policy=POLICY,
        sleep=lambda _: None,
    )


REGULAR_OPEN = BOUNDARY - DAY - 6 * HOUR


def table() -> dict[str, object]:
    return {
        "range": [BOUNDARY - 10 * DAY, BOUNDARY + 30 * DAY],
        "calendars": {"XNYS": [[REGULAR_OPEN, REGULAR_OPEN + 6 * HOUR, "regular"]]},
    }


# --- allowlist -----------------------------------------------------------------------


def test_preserved_snapshot_hash_and_allowlist_counts() -> None:
    snapshot = load_snapshot(ROOT / SNAPSHOT_DIR)
    pool = json.loads((ROOT / "docs/reports/D98-pool.json").read_text(encoding="utf-8"))["body"]
    result = build(snapshot, crypto_assets(pool))
    assert len(result.entries) == 305 and not result.rejected
    assert sum(e.launch >= BOUNDARY for e in result.entries.values()) == 81


def test_tampered_snapshot_is_rejected(tmp_path: Path) -> None:
    copy = tmp_path / "snap"
    shutil.copytree(ROOT / SNAPSHOT_DIR, copy)
    (copy / "forex.json").write_bytes((copy / "forex.json").read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_snapshot(copy)


def row(symbol: str, kind: str = "stock", base: str = "XQ", **extra: object) -> dict[str, object]:
    return {
        "symbol": symbol,
        "symbolType": kind,
        "baseCoin": base,
        "contractType": "LinearPerpetual",
        "settleCoin": "USDT",
        "quoteCoin": "USDT",
        "status": "Trading",
        "isPreListing": False,
        "launchTime": str(OLD),
        "symbolId": "7",
        **extra,
    }


def test_unknown_type_stops_crypto_overlap_rejected_and_changes_held() -> None:
    with pytest.raises(ValueError, match="unknown symbolType"):
        build({"stock": [row("XQ1USDT", kind="crypto")]}, (frozenset(), frozenset()))
    crypto = (frozenset({"XQCOINUSDT"}), frozenset({"XQCOIN2"}))
    result = build(
        {
            "stock": [
                row("XQ1USDT"),
                row("XQ2USDT"),
                row("XQCOINUSDT"),
                row("XQ3USDT", base="XQCOIN2"),
            ]
        },
        crypto,
    )
    assert result.rejected == {"XQCOINUSDT": "d98_crypto_overlap", "XQ3USDT": "d98_crypto_overlap"}
    hold_changed(result, {"stock": [row("XQ1USDT", symbolId="8"), row("XQ4USDT")]})
    assert result.held == {"XQ1USDT": "identity_changed", "XQ2USDT": "missing_now"}
    assert result.symbols == frozenset() and "XQ4USDT" not in result.entries


# --- guard ---------------------------------------------------------------------------

REJECTED = [
    "/v5/market/tickers?category=linear&symbol=XQAUSDT",
    "/v5/market/tickers?category=linear",
    "/v5/market/orderbook?category=linear&limit=1",
    "/v5/market/orderbook?category=linear&symbol=XQZZUSDT&limit=1",
    f"/v5/market/kline?category=linear&symbol=XQAUSDT&interval=60&start={OLD}&limit=1000",
    f"/v5/market/kline?category=linear&symbol=XQAUSDT&interval=60&start={OLD}"
    f"&end={BOUNDARY - HOUR + 1}&limit=1000",
    f"/v5/market/kline?category=linear&symbol=XQAUSDT&interval=5&start={OLD}"
    f"&end={BOUNDARY - 1}&limit=1000",
    f"/v5/market/index-price-kline?category=linear&symbol=XQAUSDT&interval=5&start={OLD}"
    f"&end={OLD}&limit=1000",
    f"/v5/market/funding/history?category=linear&symbol=XQAUSDT&endTime={OLD}&limit=200",
    f"/v5/market/funding/history?category=linear&symbol=XQAUSDT&startTime={OLD}"
    f"&endTime={BOUNDARY}&limit=200",
    f"/v5/market/kline?category=linear&symbol=XQBUSDT&interval=60&start={OLD}&end={OLD}&limit=1000",
    f"/v5/market/funding/history?category=linear&symbol=XQBUSDT&startTime={OLD}"
    f"&endTime={OLD}&limit=200",
    "/v5/market/instruments-info?category=linear&limit=1000",
]


@pytest.mark.parametrize("query", REJECTED)
def test_guard_rejects_before_network(query: str) -> None:
    transport = FakeTransport()
    with pytest.raises(ValueError):
        StructureTransport(transport, launches()).get(API + query, 1)
    assert transport.urls == []


def test_guard_allows_bounded_history_and_live_book_for_new_symbol() -> None:
    transport = FakeTransport()
    guard = StructureTransport(transport, launches())
    guard.get(
        API + "/v5/market/kline?category=linear&symbol=XQAUSDT&interval=60"
        f"&start={OLD}&end={BOUNDARY - HOUR}&limit=1000",
        1,
    )
    guard.get(
        API + "/v5/market/funding/history?category=linear&symbol=XQAUSDT"
        f"&startTime={OLD}&endTime={BOUNDARY - 1}&limit=200",
        1,
    )
    guard.get(API + "/v5/market/orderbook?category=linear&symbol=XQBUSDT&limit=1", 1)
    guard.get(API + "/v5/market/instruments-info?category=linear&symbolType=ETF&limit=1000", 1)
    assert len(transport.urls) == 4


def test_responses_reaching_b_are_incidents_and_unknown_book_fields_stop() -> None:
    leaky = client(FakeTransport(leak=True))
    with pytest.raises(ObservationIncident):
        leaky.bars("/v5/market/kline", "XQAUSDT", "60", OLD, BOUNDARY - HOUR)
    with pytest.raises(ObservationIncident):
        leaky.funding("XQAUSDT", OLD, BOUNDARY - 1)
    with pytest.raises(ValueError, match="unexpected orderbook fields"):
        client(FakeTransport(book_extra=True)).orderbook("XQAUSDT", "1")


# --- collection ----------------------------------------------------------------------


def test_history_requests_stay_below_b_and_skip_new_symbols(tmp_path: Path) -> None:
    transport = FakeTransport()
    store = Store(tmp_path / "d103.sqlite")
    collect_history(store, client(transport), allowlist(), table(), code_commit="c")
    assert transport.urls and not any("XQBUSDT" in u for u in transport.urls)
    assert not any("tickers" in u for u in transport.urls)
    for url in transport.urls:
        p = dict(parse_qsl(urlsplit(url).query))
        assert int(p.get("end") or p["endTime"]) <= BOUNDARY - 1
    starts = [s for (s,) in store.rows("SELECT start FROM bars WHERE interval='60'")]
    assert max(starts) == BOUNDARY - HOUR and min(starts) == OLD
    funding = [t for (t,) in store.rows("SELECT time FROM funding")]
    assert funding and max(funding) < BOUNDARY
    five = sorted(s for (s,) in store.rows("SELECT start FROM bars WHERE interval='5'"))
    assert five == [REGULAR_OPEN + k * 5 * MINUTE for k in (-1, 0, 1, 2)]
    assert {k for k, *_ in history_tasks(allowlist())} == {"trade", "index", "mark", "funding"}
    store.close()


def test_incident_stores_nothing_logs_and_stops(tmp_path: Path) -> None:
    store = Store(tmp_path / "d103.sqlite")
    with pytest.raises(ObservationIncident):
        collect_history(
            store, client(FakeTransport(leak=True)), allowlist(), table(), code_commit="c"
        )
    assert list(store.rows("SELECT COUNT(*) FROM bars")) == [(0,)]
    logs = [json.loads(c) for (c,) in store.rows("SELECT content FROM log")]
    assert any(e.get("event") == "observation_incident" for e in logs)
    store.close()


def test_live_uses_orderbook_only_binds_t0_and_samples_depth_hourly(tmp_path: Path) -> None:
    transport = FakeTransport()
    store = Store(tmp_path / "d103.sqlite")
    start = BOUNDARY + 30 * DAY + HOUR - 5 * MINUTE
    now = [start]

    def sleep(seconds: float) -> None:
        now[0] += int(seconds * 1000)

    collect_live(
        store, client(transport), allowlist(), clock=lambda: now[0], sleep=sleep, stop_after=2
    )
    assert transport.urls and all("/v5/market/orderbook" in u for u in transport.urls)
    assert store.get("T0") == str(start)
    assert list(store.rows("SELECT COUNT(*) FROM book_top")) == [(4,)]
    assert list(store.rows("SELECT COUNT(*) FROM book_depth")) == [(2,)]
    store.close()


def test_store_refuses_d98_paths(tmp_path: Path) -> None:
    for path in (tmp_path / "d98-design.sqlite", tmp_path / "d98-code-C" / "x.sqlite"):
        with pytest.raises(ValueError, match="D98"):
            Store(path)
    assert not (tmp_path / "d98-design.sqlite").exists()


def test_data_hash_is_order_independent(tmp_path: Path) -> None:
    a, b = Store(tmp_path / "a.sqlite"), Store(tmp_path / "b.sqlite")
    rows = [("XQAUSDT", 2, "0.1"), ("XQAUSDT", 1, "0.2")]
    a.put("funding", rows)
    b.put("funding", rows[::-1])
    assert a.data_hash() == b.data_hash()
    a.close()
    b.close()


# --- sessions ------------------------------------------------------------------------


def fake_wall(day: date, hour: int, minute: int) -> int:
    """New York wall time with a DST switch on 2026-03-08 (UTC-5 to UTC-4)."""
    offset = 4 if day >= date(2026, 3, 8) else 5
    return (day - date(1970, 1, 1)).days * DAY + (hour + offset) * HOUR + minute * MINUTE


def test_us_extended_follows_dst_early_close_and_weekend_gap() -> None:
    friday, monday = date(2026, 3, 6), date(2026, 3, 9)
    days = [
        (friday, fake_wall(friday, 9, 30), fake_wall(friday, 16, 0)),
        (monday, fake_wall(monday, 9, 30), fake_wall(monday, 13, 0)),
    ]
    sessions = Sessions(us_extended(days, fake_wall), 0, 10**14)
    assert days[0][1] % DAY == 14 * HOUR + 30 * MINUTE  # EST
    assert days[1][1] % DAY == 13 * HOUR + 30 * MINUTE  # EDT
    assert sessions.label(fake_wall(date(2026, 3, 7), 12, 0)) == "closed"
    assert sessions.label(fake_wall(monday, 14, 0)) == "post"
    assert sessions.label(fake_wall(date(2026, 3, 8), 21, 0)) == "overnight"
    kinds = {(b.previous, b.label) for b in sessions.boundaries()}
    assert ("closed", "overnight") in kinds and ("pre", "regular") in kinds
    assert sessions.label(-1) == "unknown"


def test_holiday_gap_is_closed_and_overlap_rejected() -> None:
    sessions = Sessions([(0, HOUR, "regular"), (2 * DAY, 2 * DAY + HOUR, "regular")], 0, 9 * DAY)
    assert sessions.label(DAY) == "closed"
    assert [b.previous for b in sessions.boundaries()] == ["closed"]
    with pytest.raises(ValueError, match="overlapping"):
        Sessions([(0, 10, "regular"), (5, 20, "regular")], 0, 100)
    with pytest.raises(ValueError):
        us_extended([(date(2026, 3, 9), fake_wall(date(2026, 3, 9), 3, 0), 0)], fake_wall)


# --- statistics ----------------------------------------------------------------------


def test_turnover_uses_complete_days_and_thirty_consecutive_days() -> None:
    hourly = {d * DAY + h * HOUR: D(1) for d in range(31) for h in range(24)}
    del hourly[5 * DAY]
    daily = stats.daily_turnover(hourly)
    assert 5 * DAY not in daily and daily[0] == 24
    assert stats.rolling_means(daily) == {}
    hourly[5 * DAY] = D(1)
    assert stats.rolling_means(stats.daily_turnover(hourly)) == {30 * DAY: 24, 31 * DAY: 24}
    assert stats.tier(D(20_000_000)) == "20M-50M" and stats.tier(None) == "uncomputable"


def test_depth_cost_both_sides_non_negative_and_unfilled() -> None:
    asks = [["10", "50"], ["11", "1000"]]
    bids = [["9", "50"], ["8", "1000"]]
    mid = D("9.5")
    buy = stats.depth_cost(asks, mid, D(1000), "buy")
    sell = stats.depth_cost(bids, mid, D(1000), "sell")
    assert buy is not None and sell is not None and buy > 0 and sell > 0
    assert stats.depth_cost(asks[:1], mid, D(1000), "buy") is None
    assert stats.spread("9", None) is None and stats.spread("9", "11") == D("0.2")


def test_reopen_changes_skip_missing_and_flat_runs_break() -> None:
    r = 10 * DAY
    closes = {r - 5 * MINUTE: D(100), r + 10 * MINUTE: D(97)}
    values, missing = stats.reopen_changes(
        closes, [Boundary(r, "regular", "closed"), Boundary(r + DAY, "regular", "closed")]
    )
    assert values == [("regular", "closed", D("0.03"))] and missing == 1
    sessions = Sessions([(0, HOUR, "regular")], 0, 100 * HOUR)
    flat = (D(1), D(1), D(1), D(1))
    bars = {h * HOUR: flat for h in (0, 1, 2, 3, 5, 6)}
    bars[7 * HOUR] = (D(1), D(2), D(1), D(1))
    assert stats.flat_runs(bars, sessions) == [3, 2]


def test_funding_uses_current_cap_only() -> None:
    summary = stats.funding_summary([D("0.02"), D("-0.001"), D("0.0005")], "0.02")
    assert summary["at_current_cap_ratio"] == D(1) / 3
    assert summary["abs_median"] == D("0.001")
    assert stats.quantile([], D("0.5")) is None
