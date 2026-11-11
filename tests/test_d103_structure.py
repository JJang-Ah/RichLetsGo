"""D103 stage-0 collection guards and structure statistics on synthetic data only.

Synthetic symbols use an XQ prefix so they never name a real (or D98 judgment) contract.
"""

import json
import shutil
from collections.abc import Callable
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
from richletsgo.research.d103.guard import (
    FUNDING_WINDOW,
    ObservationIncident,
    StructureClient,
    StructureTransport,
)
from richletsgo.research.d103.models import BOUNDARY, DAY, HOUR, LIVE_SPAN, MINUTE, SNAPSHOT_DIR
from richletsgo.research.d103.sessions import (
    Boundary,
    Sessions,
    regular_intervals,
    reopen_points,
    us_extended,
)
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

    def __init__(
        self,
        *,
        leak: bool = False,
        leak_to: int = BOUNDARY,
        book_extra: bool = False,
        empty: bool = False,
        on_book: Callable[[], None] | None = None,
    ) -> None:
        self.urls: list[str] = []
        self.leak = leak
        self.leak_to = leak_to
        self.book_extra = book_extra
        self.empty = empty
        self.on_book = on_book

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
                starts += list(range(BOUNDARY, self.leak_to + 1, width))
            if self.empty:
                starts = []
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
            if self.empty:
                times = []
            result = {
                "list": [
                    {"symbol": p["symbol"], "fundingRateTimestamp": str(t), "fundingRate": "0.0001"}
                    for t in times
                ]
            }
        elif u.path == "/v5/market/orderbook":
            if self.on_book is not None:
                self.on_book()
            result = {"s": p["symbol"], "b": [["9.9", "100"]], "a": [["10.1", "100"]], "ts": 1}
            if self.book_extra:
                result["price24hPcnt"] = "0.1"
        else:
            result = {"list": []}
        return 200, json.dumps({"retCode": 0, "result": result}).encode()


def client(
    transport: FakeTransport,
    clock: Callable[[], int] = lambda: 0,
    policy: RetryPolicy = POLICY,
) -> StructureClient:
    return StructureClient(
        API,
        transport=StructureTransport(transport, launches(), clock=clock),
        policy=policy,
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
    guard = StructureTransport(transport, launches(), clock=lambda: 0)
    with pytest.raises(ValueError, match="live window"):
        guard.get(API + "/v5/market/orderbook?category=linear&symbol=XQBUSDT&limit=1", 1)
    guard.book_until = 1
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
    extra = client(FakeTransport(book_extra=True))
    extra.guard.book_until = 1
    with pytest.raises(ObservationIncident) as caught:
        extra.orderbook("XQAUSDT", "1")
    assert caught.value.details["fields"] == ["price24hPcnt"]


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
    values, missing, halted = stats.reopen_changes(
        closes, [Boundary(r, "regular", "closed"), Boundary(r + DAY, "regular", "closed")]
    )
    assert values == [("regular", "closed", D("0.03"))] and (missing, halted) == (1, 0)
    # Both end bars exist, but an announced halt inside the window excludes it.
    values, missing, halted = stats.reopen_changes(
        closes, [Boundary(r, "regular", "closed")], [(r + MINUTE, r + 2 * MINUTE)]
    )
    assert (values, missing, halted) == ([], 0, 1)
    with pytest.raises(ValueError):
        stats.load_halts({"checked_at": "x", "halts": {"XQAUSDT": [{"start": 1, "end": 2}]}})
    assert stats.load_halts({"checked_at": "x", "halts": {}}) == {}
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


# --- PR #61 review cases --------------------------------------------------------------


def test_incident_records_observed_range_per_leak(tmp_path: Path) -> None:
    ends = []
    for n, leak_to in enumerate((BOUNDARY, BOUNDARY + 3 * HOUR)):
        store = Store(tmp_path / f"d103-{n}.sqlite")
        with pytest.raises(ObservationIncident):
            collect_history(
                store,
                client(FakeTransport(leak=True, leak_to=leak_to)),
                allowlist(),
                table(),
                code_commit="c",
            )
        logs = [json.loads(c) for (c,) in store.rows("SELECT content FROM log")]
        incident = next(e for e in logs if e["event"] == "observation_incident")
        assert incident["symbol"] == "XQAUSDT" and incident["params"]["end"]
        assert incident["detected_at"]
        ends.append(incident["observed_last_end"])
        store.close()
    assert ends == [BOUNDARY + HOUR, BOUNDARY + 4 * HOUR]


def test_empty_pages_retry_then_stay_missing_not_done(tmp_path: Path) -> None:
    transport = FakeTransport(empty=True)
    store = Store(tmp_path / "d103.sqlite")
    collect_history(
        store,
        client(transport, policy=RetryPolicy(0, 2, 0, 1)),
        allowlist(),
        table(),
        code_commit="c",
    )
    tasks = len(history_tasks(allowlist()))
    assert len(transport.urls) == 2 * tasks
    assert list(store.rows("SELECT COUNT(*) FROM pages")) == [(0,)]
    logs = [json.loads(c) for (c,) in store.rows("SELECT content FROM log")]
    assert sum(e["event"] == "missing" for e in logs) == tasks
    store.close()


def test_five_minute_page_without_windows_is_done_with_received_range(tmp_path: Path) -> None:
    store = Store(tmp_path / "d103.sqlite")
    quiet = {"range": table()["range"], "calendars": {"XNYS": []}}
    collect_history(store, client(FakeTransport()), allowlist(), quiet, code_commit="c")
    pages = list(store.rows("SELECT key, rows, received, first, last FROM pages"))
    five = [p for p in pages if json.loads(p[0])[1] == "5"]
    assert five and all(
        rows == 0 and received > 0 and first <= last for _, rows, received, first, last in five
    )
    store.close()


def test_live_never_requests_after_expiry(tmp_path: Path) -> None:
    t0 = BOUNDARY + DAY + 1234
    store = Store(tmp_path / "d103.sqlite")
    store.bind("allowlist_sha256", allowlist().sha256())
    store.bind("T0", str(t0))
    transport = FakeTransport()
    now = [t0 + LIVE_SPAN + 1]
    collect_live(
        store,
        client(transport, lambda: now[0]),
        allowlist(),
        clock=lambda: now[0],
        sleep=lambda _: None,
    )
    assert transport.urls == []
    store.close()


def test_live_expiry_mid_slot_keeps_taken_books_and_logs_unsampled(tmp_path: Path) -> None:
    t0 = BOUNDARY + DAY + 1234
    store = Store(tmp_path / "d103.sqlite")
    store.bind("allowlist_sha256", allowlist().sha256())
    store.bind("T0", str(t0))
    now = [t0 + LIVE_SPAN - 1]

    def tick() -> None:
        now[0] += 2  # the first request pushes the clock past T0 + LIVE_SPAN

    transport = FakeTransport(on_book=tick)
    collect_live(
        store,
        client(transport, lambda: now[0]),
        allowlist(),
        clock=lambda: now[0],
        sleep=lambda _: None,
    )
    assert len(transport.urls) == 1
    assert [s for (s,) in store.rows("SELECT symbol FROM book_top")] == ["XQAUSDT"]
    logs = [json.loads(c) for (c,) in store.rows("SELECT content FROM log")]
    expired = next(e for e in logs if e["event"] == "live_expired_in_slot")
    # The slot is on the hour: XQAUSDT kept its top book but expiry stopped its depth book.
    assert expired["unsampled"] == ["XQAUSDT", "XQBUSDT"]
    assert list(store.rows("SELECT COUNT(*) FROM book_depth")) == [(0,)]
    store.close()


def test_book_session_uses_response_ts_and_drops_stale(tmp_path: Path) -> None:
    sessions = Sessions([(0, HOUR, "pre"), (HOUR, 2 * HOUR, "regular")], 0, DAY)
    assert stats.book_label(sessions, HOUR - MINUTE // 2, HOUR + 1) == "regular"
    assert stats.book_label(sessions, HOUR - 5 * MINUTE, HOUR - 4 * MINUTE) == "pre"
    assert stats.book_label(sessions, 10 * MINUTE, 10 * MINUTE - 2 * MINUTE) == "stale"
    store = Store(tmp_path / "d103.sqlite")
    open_ = REGULAR_OPEN
    slot = open_ - 5 * MINUTE  # slot in the gap before the open, book taken after it
    store.put(
        "book_top",
        [
            ("XQAUSDT", slot, open_ - 1, open_ + 1, "9", "1", "11", "1"),
            ("XQAUSDT", slot + 5 * MINUTE, open_, open_ - 2 * MINUTE, "9", "1", "11", "1"),
        ],
    )
    summary = stats.summarize(store, allowlist(), table(), {})["symbols"]["XQAUSDT"]
    assert summary["spread"]["regular"]["count"] == 1 and "closed" not in summary["spread"]
    assert summary["spread_empty"] == {"stale": 1}
    store.close()


def test_reopen_points_follow_contract_and_observed_range() -> None:
    days = [
        (date(2026, 8, 3), fake_wall(date(2026, 8, 3), 9, 30), fake_wall(date(2026, 8, 3), 16, 0))
    ]
    sessions = Sessions(us_extended(days, fake_wall), 0, 10**14)
    kinds = {(b.previous, b.label) for b in reopen_points(sessions, 0)}
    assert kinds == {("closed", "overnight"), ("overnight", "pre"), ("pre", "regular")}
    lunch = Sessions(regular_intervals([[0, HOUR, 2 * HOUR, 3 * HOUR]]), -DAY, DAY)
    assert {(b.previous, b.label) for b in reopen_points(lunch, -DAY)} == {
        ("closed", "regular"),
        ("break", "regular"),
    }
    late = Sessions([(BOUNDARY - 10 * MINUTE, BOUNDARY + HOUR, "regular")], 0, 10**14)
    assert reopen_points(late, 0) == []
    # R - 5m before launch drops the first open only.
    assert [(b.previous, b.label) for b in reopen_points(lunch, 1)] == [("break", "regular")]
    assert FUNDING_WINDOW == 199 * HOUR
