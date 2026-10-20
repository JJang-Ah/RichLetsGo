"""M2-01 market data ingestion. No test touches the network (fixtures + fake exchange)."""

import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from richletsgo.domain import Symbol
from richletsgo.marketdata import ingest as ingest_module
from richletsgo.marketdata.bybit_public import (
    BybitApiError,
    BybitPublicClient,
    RetryPolicy,
    TransportError,
)
from richletsgo.marketdata.ingest import ingest_bars, ingest_funding, main, run_ingest
from richletsgo.marketdata.models import Bar, Interval, RiskLimitTier, from_ms, to_ms
from richletsgo.marketdata.store import MarketDataStore
from richletsgo.marketdata.validate import funding_gaps, parse_funding, parse_klines

FIXTURES = Path(__file__).parent / "fixtures" / "bybit"
BTC = Symbol.BTCUSDT
HOUR = timedelta(hours=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("tests must not open network connections")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ok(result):
    return 200, json.dumps({"retCode": 0, "retMsg": "OK", "result": result}).encode()


class FakeBybit:
    """Synthetic exchange: hourly bars and 8h funding, with optional holes."""

    def __init__(self, now, *, missing=(), funding_start=None, funding_missing=()):
        self.now = now
        self.missing = set(missing)
        self.funding_start = funding_start or T0
        self.funding_missing = set(funding_missing)
        self.urls = []

    def bar_row(self, start):
        base = D(100) + D(to_ms(start) % 997) / 100
        return [
            str(to_ms(start)),
            str(base),
            str(base + 2),
            str(base - 2),
            str(base + 1),
            "1.5",
            "150",
        ]

    def get(self, url, timeout):
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == "/v5/market/kline":
            step = Interval(q["interval"]).duration
            start, end = from_ms(int(q["start"])), from_ms(int(q["end"]))
            rows, cursor = [], start
            while cursor <= end and cursor <= self.now:
                if cursor not in self.missing:
                    rows.append(self.bar_row(cursor))  # includes the in-progress bar
                cursor += step
            return ok({"symbol": q["symbol"], "list": list(reversed(rows))[: int(q["limit"])]})
        if parsed.path == "/v5/market/funding/history":
            assert "startTime" not in q, "Bybit rejects startTime alone"
            end = from_ms(int(q["endTime"]))
            times, cursor = [], self.funding_start
            while cursor <= min(end, self.now):
                if cursor not in self.funding_missing:
                    times.append(cursor)
                cursor += timedelta(hours=8)
            page = sorted(times, reverse=True)[: int(q["limit"])]
            return ok(
                {
                    "list": [
                        {
                            "symbol": q["symbol"],
                            "fundingRate": "0.0001",
                            "fundingRateTimestamp": str(to_ms(t)),
                        }
                        for t in page
                    ]
                }
            )
        if parsed.path == "/v5/market/instruments-info":
            return ok(fixture("instruments.json")["result"])
        if parsed.path == "/v5/market/risk-limit":
            return ok(fixture("risk.json")["result"])
        return 404, b"{}"


def client_for(transport, *, attempts=3):
    sleeps = []
    client = BybitPublicClient(
        "https://api.example.test",
        transport=transport,
        policy=RetryPolicy(0, attempts, 0.5, 5),
        sleep=sleeps.append,
    )
    return client, sleeps


# ------------------------------------------------------------ real response fixtures


def test_real_kline_fixture_drops_in_progress_bar_and_sorts():
    payload = fixture("kline.json")
    collected = from_ms(payload["time"])
    batch = parse_klines(payload["result"]["list"], BTC, Interval.HOUR, collected)
    assert batch.in_progress_dropped == 1
    assert [b.start for b in batch.bars] == sorted(b.start for b in batch.bars)
    assert all(b.end <= collected for b in batch.bars)
    assert batch.bars[-1].close == D("81459.3")


def test_real_instrument_and_risk_limit_fixtures():
    transport = FakeBybit(T0)
    client, _ = client_for(transport)
    snap = client.instrument(BTC, T0)
    assert (snap.tick_size, snap.qty_step, snap.funding_interval) == (
        D("0.10"),
        D("0.001"),
        timedelta(minutes=480),
    )
    assert snap.to_spec().round_qty_down(D("0.0019")) == D("0.001")
    tiers = client.risk_limit_tiers(BTC, T0)
    assert tiers[0].tier == 1 and tiers[0].mmr == D("0.0033") and tiers[0].imr == D("0.0066")
    assert [t.limit_notional for t in tiers] == sorted(t.limit_notional for t in tiers)


def test_percent_scaled_margins_fail_the_unit_check():
    with pytest.raises(ValueError):
        RiskLimitTier(BTC, 1, D("300000"), D("0.33"), D("0.66"), D("150"), T0)


# ------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "row",
    [
        ["1767225600000", "0", "1", "1", "1", "1", "1"],  # nonpositive price
        ["1767225600000", "10", "9", "8", "9", "1", "1"],  # high below open
        ["1767225600000", "10", "11", "10.5", "10", "1", "1"],  # low above close
        ["1767225600001", "10", "11", "9", "10", "1", "1"],  # not aligned
        ["1767225600000", "x", "11", "9", "10", "1", "1"],  # not a number
        ["1767225600000", "10", "11"],  # short row
    ],
)
def test_invalid_rows_are_rejected_not_stored(row):
    batch = parse_klines([row], BTC, Interval.HOUR, T0 + timedelta(days=1))
    assert not batch.bars and len(batch.rejected) == 1


def test_duplicate_rows_in_one_response():
    row = ["1767225600000", "10", "11", "9", "10", "1", "1"]
    same = parse_klines([row, list(row)], BTC, Interval.HOUR, T0 + timedelta(days=1))
    assert len(same.bars) == 1 and not same.conflicts
    other = row[:4] + ["10.5"] + row[5:]
    clash = parse_klines([row, other], BTC, Interval.HOUR, T0 + timedelta(days=1))
    assert not clash.bars and clash.conflicts == [from_ms(1767225600000)]


def test_funding_gaps_follow_recorded_times_d87():
    times = [T0 + timedelta(hours=8 * i) for i in (0, 1, 3, 4)]
    gaps = funding_gaps(times, timedelta(hours=8), T0, T0 + timedelta(hours=32))
    assert gaps == [(T0 + timedelta(hours=8), T0 + timedelta(hours=24))]
    with pytest.raises(ValueError):
        parse_funding(
            [
                {"symbol": "BTCUSDT", "fundingRate": "0.1", "fundingRateTimestamp": "0"},
                {"symbol": "BTCUSDT", "fundingRate": "0.2", "fundingRateTimestamp": "0"},
            ],
            BTC,
        )


# ------------------------------------------------------------ store


def test_decimal_text_round_trip_and_no_overwrite(tmp_path):
    bar = Bar(
        BTC, Interval.HOUR, T0, D("0.10000000"), D("0.2"), D("0.1"), D("0.15"), D("0"), D("0")
    )
    with MarketDataStore(tmp_path) as store:
        assert store.put_bars([bar]).inserted == 1
        raw = sqlite3.connect(store.path).execute("SELECT open FROM bars").fetchone()[0]
        assert raw == "0.10000000"
        assert store.load_bars(BTC, Interval.HOUR, T0, T0) == [bar]
        rescaled = Bar(
            BTC, Interval.HOUR, T0, D("0.1"), D("0.20"), D("0.1"), D("0.15"), D("0"), D("0")
        )
        assert store.put_bars([rescaled]).unchanged == 1  # same values, different scale
        changed = Bar(
            BTC, Interval.HOUR, T0, D("0.1"), D("0.2"), D("0.1"), D("0.16"), D("0"), D("0")
        )
        later = Bar(BTC, Interval.HOUR, T0 + HOUR, D("1"), D("1"), D("1"), D("1"), D("0"), D("0"))
        result = store.put_bars([later, changed])
        assert not result.ok and result.inserted == 0
        assert store.load_bars(BTC, Interval.HOUR, T0, T0 + HOUR) == [bar]  # batch rolled back


# ------------------------------------------------------------ ingestion


def test_paged_fetch_then_rerun_requests_only_missing(tmp_path):
    now = T0 + timedelta(hours=2500, minutes=30)  # mid-hour: last bar in progress
    exchange = FakeBybit(now)
    client, _ = client_for(exchange)
    with MarketDataStore(tmp_path) as store:
        first = ingest_bars(client, store, BTC, Interval.HOUR, T0, now)
        assert first.ok and first.stored == 2500 and not first.gaps
        assert len(exchange.urls) == 3  # 1000 bars per page
        assert store.bar_starts(BTC, Interval.HOUR, T0, now)[-1] == T0 + timedelta(hours=2499)
        exchange.urls.clear()
        again = ingest_bars(client, store, BTC, Interval.HOUR, T0, now)
        assert again.ok and again.requested_ranges == [] and exchange.urls == []
        later = now + timedelta(hours=3)
        exchange.now = later
        more = ingest_bars(client, store, BTC, Interval.HOUR, T0, later)
        assert more.stored == 3 and more.requested_ranges == [
            ("2026-04-15T04:00:00Z", "2026-04-15T06:00:00Z")
        ]


def test_exchange_holes_are_reported_not_filled(tmp_path):
    now = T0 + timedelta(hours=48)
    hole = {T0 + timedelta(hours=10), T0 + timedelta(hours=11)}
    exchange = FakeBybit(now, missing=hole)
    client, _ = client_for(exchange)
    with MarketDataStore(tmp_path) as store:
        report = ingest_bars(client, store, BTC, Interval.HOUR, T0, now)
        assert report.gaps == [("2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z")]
        assert len(store.bar_starts(BTC, Interval.HOUR, T0, now)) == 46
        exchange.urls.clear()
        retry = ingest_bars(client, store, BTC, Interval.HOUR, T0, now)
        assert retry.requested_ranges == [("2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z")]
        assert len(exchange.urls) == 1


def test_conflicting_duplicate_from_exchange_fails_the_series(tmp_path):
    now = T0 + timedelta(hours=6)

    class Duplicating(FakeBybit):
        def get(self, url, timeout):
            status, body = super().get(url, timeout)
            if "/kline" in url:
                payload = json.loads(body)
                rows = payload["result"]["list"]
                clash = list(next(r for r in rows if int(r[0]) == to_ms(T0 + 3 * HOUR)))
                clash[4] = str(D(clash[4]) + 1)
                rows.append(clash)
                body = json.dumps(payload).encode()
            return status, body

    client, _ = client_for(Duplicating(now))
    with MarketDataStore(tmp_path) as store:
        report = ingest_bars(client, store, BTC, Interval.HOUR, T0, now)
        assert not report.ok and report.conflicts == ["response 2026-01-01T03:00:00Z"]
        starts = store.bar_starts(BTC, Interval.HOUR, T0, now)
        assert T0 + 3 * HOUR not in starts and len(starts) == 5
        assert report.gaps == [("2026-01-01T03:00:00Z", "2026-01-01T03:00:00Z")]


def test_funding_pages_backwards_and_reports_gaps(tmp_path):
    now = T0 + timedelta(hours=8 * 300)
    missing = {T0 + timedelta(hours=8 * 50)}
    exchange = FakeBybit(now, funding_missing=missing)
    client, _ = client_for(exchange)
    with MarketDataStore(tmp_path) as store:
        report = ingest_funding(client, store, BTC, T0, now, timedelta(hours=8))
        assert report.stored == 300  # 301 slots minus one hole
        assert len([u for u in exchange.urls if "funding" in u]) == 2  # 200 + 101
        assert report.gaps == [("2026-01-17T08:00:00Z", "2026-01-18T00:00:00Z")]


def test_snapshots_and_full_run(tmp_path):
    now = T0 + timedelta(days=3)
    exchange = FakeBybit(now)
    client, _ = client_for(exchange)
    with MarketDataStore(tmp_path) as store:
        result = run_ingest(
            client,
            store,
            symbols=[BTC],
            intervals=[Interval.DAY, Interval.HOUR],
            since=T0,
            now=now,
            minute_range=None,
            with_funding=True,
        )
        assert result.ok, [(r.name, r.error, r.conflicts) for r in result.reports]
        assert store.latest_instrument(BTC).funding_interval == timedelta(hours=8)
        assert store.latest_risk_tiers(BTC)[0].mmr == D("0.0033")
        assert len(store.bar_starts(BTC, Interval.DAY, T0, now)) == 3


# ------------------------------------------------------------ retries


class Scripted:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, timeout):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_retry_on_transport_error_then_success():
    transport = Scripted(TransportError("reset"), (503, b""), ok({"list": []}))
    client, sleeps = client_for(transport)
    assert client.klines(BTC, Interval.HOUR, T0, T0) == []
    assert transport.calls == 3
    assert [s for s in sleeps if s] == [0.5, 1.0]  # exponential backoff


@pytest.mark.parametrize(
    "responses,calls",
    [
        (
            [
                (200, json.dumps({"retCode": 10006, "retMsg": "rate"}).encode()),
                (200, json.dumps({"retCode": 10006, "retMsg": "rate"}).encode()),
                (200, json.dumps({"retCode": 10006, "retMsg": "rate"}).encode()),
            ],
            3,
        ),
        ([(429, b""), (429, b""), (429, b"")], 3),
        ([(400, b"bad request")], 1),
    ],
)
def test_failures_after_retries_or_immediately(responses, calls):
    transport = Scripted(*responses)
    client, _ = client_for(transport)
    with pytest.raises(BybitApiError):
        client.klines(BTC, Interval.HOUR, T0, T0)
    assert transport.calls == calls


# ------------------------------------------------------------ CLI and paths


def test_cli_uses_config_relative_path_regardless_of_cwd(tmp_path, monkeypatch):
    config_dir = tmp_path / "설정 폴더"  # spaces and Hangul (03 3.1)
    config_dir.mkdir()
    config = config_dir / "config.local.toml"
    config.write_text(
        '[bybit_public]\nbase_url = "https://api.example.test"\n'
        '[paths.backtest]\nmarket_data_dir = "data/market"\n',
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    now = T0 + timedelta(hours=30)
    exchange = FakeBybit(now)
    code = main(
        [
            "--config",
            str(config),
            "--symbols",
            "BTCUSDT",
            "--intervals",
            "60",
            "--since",
            "2026-01-01",
        ],
        now=lambda: now,
        transport_factory=lambda: exchange,
        sleep=lambda _s: None,
    )
    assert code == 0
    db = config_dir / "data" / "market" / "market_data.sqlite3"
    assert db.exists() and not (elsewhere / "data").exists()
    runs = sqlite3.connect(db).execute("SELECT ok, code_commit FROM ingest_runs").fetchall()
    assert runs == [(1, "unknown")]


def test_minute_bars_require_explicit_range():
    with pytest.raises(SystemExit):
        main(["--config", "x.toml", "--symbols", "BTCUSDT", "--intervals", "1"])
    assert ingest_module.DEFAULT_HISTORY == timedelta(days=3 * 365)
