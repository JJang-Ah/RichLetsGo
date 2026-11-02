"""Offline D98 collection/audit contracts; fixtures never read real prices."""

import json
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from richletsgo.backtest.codec import digest, encode
from richletsgo.marketdata.bybit_public import RetryPolicy
from richletsgo.research.d98.audit import audit, coverage, series_hash
from richletsgo.research.d98.boundaries import (
    BTC_START,
    DAY,
    END,
    HOUR,
    START,
    STUDY_START,
    design_contracts,
    load_pool,
    window,
)
from richletsgo.research.d98.collect import DesignClient, DesignTransport, collect
from richletsgo.research.d98.store import Store, dataset_lock


def contract(symbol="AAAUSDT", launch=START, delist=None, key="AAA"):
    return dict(
        symbol=symbol,
        launch=launch,
        delist=delist,
        key=key,
        status="Closed" if delist else "Trading",
        pool="DESIGN",
        excluded_reason=None,
        priority=[1, launch, symbol],
    )


def pool(*contracts):
    cs = list(contracts)
    if not any(c["symbol"] == "BTCUSDT" for c in cs):
        cs.append(contract("BTCUSDT", key="BTC"))
    body = {
        "rule_version": "D98-pool-v1",
        "contracts": cs,
        "instruments": [{"symbol": c["symbol"], "fundingInterval": "480"} for c in cs],
    }
    return {"body": body, "sha256": digest(body)}


def bars(times, value="100"):
    return [[t, value, value, value, value, "1", "100"] for t in times]


@pytest.mark.parametrize(
    "launch,delist,first,end",
    [
        (START - DAY, None, START, END),
        (START + HOUR + 1, None, START + HOUR, END),
        (START, START + 8 * HOUR + 30_000, START, START + 8 * HOUR + 30_000),
        (START - DAY, START, START, START),
    ],
)
def test_boundaries(launch, delist, first, end):
    w = window(contract(launch=launch, delist=delist))
    assert (w.start, w.end) == (first, end)
    if delist and not w.empty:
        assert w.contains(delist, "funding")
        assert not w.contains(delist + 1, "funding")
        assert not w.contains(delist // HOUR * HOUR, "bars")


def test_digest_and_design_rejection(tmp_path):
    p = pool(contract())
    p["body"]["contracts"][0]["pool"] = "JUDGMENT"
    path = tmp_path / "pool.json"
    path.write_text(encode(p))
    with pytest.raises(ValueError, match="digest"):
        load_pool(path)
    p["sha256"] = digest(p["body"])
    assert "AAAUSDT" not in design_contracts(p)
    st = Store(tmp_path / "a.sqlite")

    class NoCalls:
        def get(self, *a):
            raise AssertionError("network reached")

    client = DesignClient(
        "https://api.bybit.com",
        transport=DesignTransport(NoCalls(), {"BTCUSDT"}),
        policy=RetryPolicy(0, 1, 0, 1),
        sleep=lambda _: None,
    )
    for symbol in ("AAAUSDT", "UNKNOWNUSDT"):
        with pytest.raises(ValueError, match="design"):
            collect(p, st, client, symbols=[symbol])
        with pytest.raises(ValueError):
            client.page(symbol, "bars", START, START, {"BTCUSDT"})
        with pytest.raises(ValueError):
            client._transport.get(
                "https://api.bybit.com/v5/market/kline?category=linear&symbol=" + symbol, 1
            )
    for endpoint in (
        "tickers",
        "instruments-info",
        "funding/history?category=linear&symbol=BTCUSDT&extra=x",
    ):
        with pytest.raises(ValueError):
            client._transport.get("https://api.bybit.com/v5/market/" + endpoint, 1)
    st.close()


def test_store_conflict_freeze_lock(tmp_path):
    st = Store(tmp_path / "x.sqlite")
    with st.db:
        st.put("bars", "A", bars([START]))
    with st.db:
        st.put("bars", "A", bars([START], "100.00"))
    with pytest.raises(ValueError, match="conflicting"), st.db:
        st.put("bars", "A", bars([START + HOUR, START], "101"))
    assert len(st.times("bars", "A")) == 1
    with dataset_lock(st.path), pytest.raises(FileExistsError):
        with dataset_lock(st.path):
            pass
    st.freeze("registration")
    with pytest.raises(ValueError, match="frozen"):
        st.put("bars", "A", [])
    st.close()


class FixtureTransport:
    def __init__(self, d, *, fail_once=False):
        self.d = d
        self.urls = []
        self.fail_once = fail_once

    def get(self, url, timeout):
        self.urls.append(url)
        if self.fail_once:
            self.fail_once = False
            return 429, b"{}"
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        symbol = q["symbol"]
        if "funding" in url:
            first, last = int(q["startTime"]), int(q["endTime"])
            rows = [
                {"symbol": symbol, "fundingRateTimestamp": str(t), "fundingRate": "0.001"}
                for t in range(START - DAY, self.d + 1, 8 * HOUR)
                if first <= t <= last
            ]
        elif q["interval"] == "D":
            rows = []
        else:
            first, last = int(q["start"]), int(q["end"])
            rows = bars(range(max(START, first), min(last + HOUR, self.d), HOUR))
        return 200, json.dumps(
            {"retCode": 0, "result": {"symbol": symbol, "category": "linear", "list": rows[::-1]}}
        ).encode()


def test_collect_resume_retry_delist_hash(tmp_path):
    d = START + 8 * HOUR
    c = contract(delist=d)
    p = pool(c)
    st = Store(tmp_path / "x.sqlite")
    transport = FixtureTransport(d, fail_once=True)
    client = DesignClient(
        "https://api.bybit.com",
        transport=DesignTransport(transport, set(design_contracts(p))),
        policy=RetryPolicy(0, 2, 0, 1),
        sleep=lambda _: None,
    )
    collect(p, st, client, symbols=["AAAUSDT"])
    assert d in st.times("funding", "AAAUSDT")
    assert len(st.times("bars", "AAAUSDT")) == 8
    report = coverage(st, c, [c], 480)
    assert report["E_a"] == d and report["tail"] == "allowed"
    n = len(transport.urls)
    collect(p, st, client, symbols=["AAAUSDT"])
    assert len(transport.urls) == n
    logs = [json.loads(r[0]) for r in st.db.execute("SELECT content FROM collection_log")]
    assert any(a["attempt"] == 2 for log in logs for a in log["attempts"])
    before = series_hash(st, "funding", "AAAUSDT", c)

    # Independent one-unit liquidation ledger for the 07b boundary regression;
    # full signal/trade simulator belongs to 07c.
    def settlement_r():
        rates = [Decimal(r[1]) for r in st.rows("funding", "AAAUSDT") if START < int(r[0]) <= d]
        return (
            Decimal("98")
            - Decimal("100")
            - Decimal("0.00055") * 198
            - sum(rate * 100 for rate in rates)
        ) / 2

    r_before = settlement_r()
    hash_before = audit(p, st.path)["body"]["data_hash"]
    with st.db:
        st.db.execute("UPDATE funding SET rate='0.002' WHERE symbol='AAAUSDT' AND time=?", (d,))
    assert before != series_hash(st, "funding", "AAAUSDT", c)
    assert settlement_r() == r_before - Decimal("0.05")
    assert hash_before != audit(p, st.path)["body"]["data_hash"]
    with pytest.raises(ValueError, match="conflicting"):
        collect(p, st, client, symbols=["AAAUSDT"], refresh=True)
    st.close()


@pytest.mark.parametrize("tail,expected", [(2, "allowed"), (24, "allowed"), (30, "excess")])
def test_tail_and_internal_gap(tmp_path, tail, expected):
    d = START + 100 * HOUR + 1234
    c = contract(delist=d)
    st = Store(tmp_path / "x.sqlite")
    with st.db:
        st.put(
            "bars",
            c["symbol"],
            bars(t for t in range(START, START + (100 - tail) * HOUR, HOUR) if t != START + HOUR),
        )
    r = coverage(st, c, [c], 480)
    assert r["L_e"] == START + 99 * HOUR and r["tail"] == expected
    assert r["missing_ranges"][0] == [START + HOUR, START + 2 * HOUR]
    assert len(r["missing_ranges"]) == (1 if expected == "allowed" else 2)
    st.close()


def test_audit_price_invariance_workers_hash(tmp_path):
    c = contract(delist=STUDY_START + DAY)
    p = pool(c)
    path = tmp_path / "x.sqlite"
    st = Store(path)
    st.bind(p["sha256"])
    with st.db:
        st.put("bars", c["symbol"], bars(range(START, c["delist"], HOUR)))
        st.put(
            "funding",
            c["symbol"],
            [[t, "0"] for t in range(START - DAY, c["delist"] + 1, 8 * HOUR)],
        )
        st.put("daily", "BTCUSDT", bars(range(BTC_START, END, DAY)))
        st.put("bars", c["symbol"], bars([START - HOUR, c["delist"]]))
    a = audit(p, path)
    b = audit(p, path, workers=2)
    assert a == b
    assert a["body"]["status"] == "READY"
    assert a["body"]["contracts"][0]["outside_bars"] == 2
    with st.db:
        st.db.execute("UPDATE bars_1h SET open='999',turnover='-888'")
    b = audit(p, path)
    assert a["body"]["contracts"] == b["body"]["contracts"]
    assert a["body"]["data_hash"] != b["body"]["data_hash"]
    with st.db:
        st.db.execute("DELETE FROM btc_daily WHERE start=?", (BTC_START,))
    assert audit(p, path)["body"]["status"] == "BTC_INCOMPLETE"
    st.close()


@pytest.mark.parametrize(
    "missing_offset,status,missing", [(19, "READY", 1), (18, "STUDY_UNDECIDABLE", 2)]
)
def test_five_percent_boundary(tmp_path, missing_offset, status, missing):
    c = contract(delist=STUDY_START + 80 * HOUR)
    p = pool(c)
    path = tmp_path / "boundary.sqlite"
    st = Store(path)
    st.bind(p["sha256"])
    hole = STUDY_START + missing_offset * 4 * HOUR - HOUR
    with st.db:
        st.put("bars", c["symbol"], bars(t for t in range(START, c["delist"], HOUR) if t != hole))
        st.put(
            "funding",
            c["symbol"],
            [[t, "0"] for t in range(START - DAY, c["delist"] + 1, 8 * HOUR)],
        )
        st.put("daily", "BTCUSDT", bars(range(BTC_START, END, DAY)))
    result = audit(p, path)["body"]
    assert result["metadata_eligible"] == 20
    assert result["input_insufficient"] == missing
    assert result["status"] == status
    st.close()


def test_lifetime_selection_and_sixty_days(tmp_path):
    c = contract(launch=START + 1, delist=STUDY_START + DAY)
    other = contract("BBBUSDT", launch=START - 1, delist=STUDY_START + 4 * HOUR, key=c["key"])
    st = Store(tmp_path / "lifetime.sqlite")
    with st.db:
        st.put("bars", c["symbol"], bars(range(START, c["delist"], HOUR)))
        st.put(
            "funding",
            c["symbol"],
            [[t, "0"] for t in range(START - DAY, c["delist"] + 1, 8 * HOUR)],
        )
    r = coverage(st, c, [c, other], 480)
    assert r["F"] == START + HOUR
    assert r["opportunities"] == 5
    assert r["metadata_eligible"] == 5
    # Actual first bar is evidence, even when metadata says the contract is old.
    with st.db:
        st.db.execute("DELETE FROM bars_1h WHERE start<?", (STUDY_START - 59 * DAY,))
    assert coverage(st, c, [c, other], 480)["metadata_eligible"] == 0
    st.close()


def test_hash_canonical_order_and_outside_rows(tmp_path):
    c = contract(delist=START + 8 * HOUR)
    st = Store(tmp_path / "order.sqlite")
    with st.db:
        st.put("bars", c["symbol"], bars(reversed(range(START, c["delist"], HOUR))))
    actual = series_hash(st, "bars", c["symbol"], c)
    import hashlib

    expected = hashlib.sha256(
        "".join(encode(r) + "\n" for r in bars(range(START, c["delist"], HOUR))).encode()
    ).hexdigest()
    assert actual == (expected, 8)
    with st.db:
        st.put("bars", c["symbol"], bars([START - HOUR, c["delist"]]))
    assert actual == series_hash(st, "bars", c["symbol"], c)
    st.close()


def test_failed_page_has_audit_without_checkpoint(tmp_path):
    p = pool(contract(delist=START + HOUR))
    st = Store(tmp_path / "failed.sqlite")

    class Broken:
        def get(self, url, timeout):
            return 503, b"{}"

    client = DesignClient(
        "https://api.bybit.com",
        transport=DesignTransport(Broken(), set(design_contracts(p))),
        policy=RetryPolicy(0, 2, 0, 1),
        sleep=lambda _: None,
    )
    from richletsgo.marketdata.bybit_public import BybitApiError

    with pytest.raises(BybitApiError):
        collect(p, st, client, symbols=["AAAUSDT"])
    assert st.db.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
    record = json.loads(st.db.execute("SELECT content FROM collection_log").fetchone()[0])
    assert len(record["attempts"]) == 2 and record["error"]
    assert not client.attempts and not client.requests
    st.close()


def test_refresh_fills_hole_and_empty_lifetime_skips(tmp_path):
    c = contract(delist=START + 8 * HOUR)
    old = contract("OLDUSDT", launch=START - 10 * DAY, delist=START, key="OLD")
    p = pool(c, old)
    transport = FixtureTransport(c["delist"])
    client = DesignClient(
        "https://api.bybit.com",
        transport=DesignTransport(transport, set(design_contracts(p))),
        policy=RetryPolicy(0, 1, 0, 1),
        sleep=lambda _: None,
    )
    st = Store(tmp_path / "refresh.sqlite")
    collect(p, st, client, symbols=["AAAUSDT", "OLDUSDT"])
    assert not any("OLDUSDT" in url for url in transport.urls)
    with st.db:
        st.db.execute("DELETE FROM bars_1h WHERE symbol=? AND start=?", ("AAAUSDT", START))
    collect(p, st, client, symbols=["AAAUSDT"], refresh=True)
    assert START in st.times("bars", "AAAUSDT")
    st.close()


@pytest.mark.parametrize("offset,eligible", [(0, 1), (1, 0)])
def test_exact_sixty_days(tmp_path, offset, eligible):
    c = contract(launch=STUDY_START - 60 * DAY + offset, delist=STUDY_START + HOUR)
    st = Store(tmp_path / "sixty.sqlite")
    with st.db:
        st.put("bars", c["symbol"], bars(range(c["launch"] // HOUR * HOUR, c["delist"], HOUR)))
    assert coverage(st, c, [c], 480)["metadata_eligible"] == eligible
    st.close()


@pytest.mark.parametrize("value", [float("nan"), True, 1.5])
def test_decimal_only_input(tmp_path, value):
    st = Store(tmp_path / "numbers.sqlite")
    with pytest.raises(ValueError), st.db:
        st.put("funding", "AAAUSDT", [[START, value]])
    st.close()
