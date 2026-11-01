"""Offline D98 pool contracts and network boundary regressions."""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from richletsgo.backtest.codec import digest, encode
from richletsgo.marketdata.bybit_public import BybitPublicClient, RetryPolicy
from richletsgo.research.d98 import tables
from richletsgo.research.d98.pool import (
    MetadataTransport,
    active_contract,
    asset_key,
    assignment,
    build,
    collect,
    markdown,
    review,
)
from tools import d98_pool

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def row(base="ABC", **changes):
    value = dict(
        symbol=base + "USDT",
        baseCoin=base,
        quoteCoin="USDT",
        settleCoin="USDT",
        contractType="LinearPerpetual",
        status="Trading",
        launchTime="1000",
        deliveryTime="0",
        fundingInterval=480,
    )
    value.update(changes)
    return value


class Fake:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        return 200, json.dumps({"retCode": 0, "result": next(self.pages)}).encode()


def client(fake):
    return BybitPublicClient(
        "https://api.bybit.com",
        transport=MetadataTransport(fake),
        policy=RetryPolicy(0, 1, 0, 1),
        sleep=lambda _: None,
    )


@pytest.mark.parametrize(
    "base,key,mult",
    [
        ("1000PEPE", "PEPE", 1000),
        ("SHIB1000", "SHIB", 1000),
        ("10000000AIDOGE", "AIDOGE", 10000000),
        ("1000", "1000", 1),
        ("1000A100", "A100", 1000),
        ("ｍａｔｉｃ", "POL", 1),
        ("RNDR", "RENDER", 1),
        ("FTM", "S", 1),
        ("BTC", "BTC", 1),
    ],
)
def test_keys(base, key, mult):
    assert asset_key(base) == (key, mult)


def test_numeric_exception():
    assert asset_key("1000SATS", frozenset({"1000SATS"})) == ("1000SATS", 1)


@pytest.mark.parametrize("key", sorted(tables.OBSERVED))
def test_observed(key):
    assert assignment(key) == "DESIGN"
    assert build([row("1000" + key)], NOW, "c")["body"]["contracts"][0]["pool"] == "DESIGN"


def test_hash_assignment():
    import hashlib

    key = "SYNTHETIC"
    expected = (
        "DESIGN" if hashlib.sha256(b"D98-pool-v1|SYNTHETIC").digest()[0] % 2 == 0 else "JUDGMENT"
    )
    assert assignment(key) == expected


@pytest.mark.parametrize("path", ["kline", "funding/history", "tickers", "risk-limit"])
def test_endpoint_denied_before_transport(path):
    fake = Fake([])
    with pytest.raises(ValueError):
        MetadataTransport(fake).get(f"https://api.bybit.com/v5/market/{path}?category=linear", 1)
    assert fake.urls == []


@pytest.mark.parametrize(
    "query",
    [
        "category=inverse&status=Trading&limit=1000",
        "category=linear&status=PreLaunch&limit=1000",
        "category=linear&status=Trading&limit=1000&category=inverse",
        "category=linear&status=Trading&limit=1000&symbol=BTCUSDT",
    ],
)
def test_parameter_allowlist(query):
    fake = Fake([])
    with pytest.raises(ValueError):
        MetadataTransport(fake).get("https://api.bybit.com/v5/market/instruments-info?" + query, 1)
    assert not fake.urls


def test_both_statuses_paginate_and_normalize():
    fake = Fake(
        [
            {"list": [row()], "nextPageCursor": "next"},
            {"list": [row("DEF")], "nextPageCursor": ""},
            {"list": [row("OLD", status="Closed", deliveryTime="9000")]},
        ]
    )
    rows = collect(client(fake))
    assert [r["symbol"] for r in rows] == ["ABCUSDT", "DEFUSDT", "OLDUSDT"]
    assert rows[0]["fundingInterval"] == "480"
    assert "cursor=next" in fake.urls[1]
    assert "status=Closed" in fake.urls[2]


def test_cursor_loop_and_duplicates_fail():
    fake = Fake([{"list": [], "nextPageCursor": "x"}] * 2)
    with pytest.raises(ValueError, match="cursor loop"):
        collect(client(fake))
    with pytest.raises(ValueError, match="duplicate"):
        build([row(), row()], NOW, "c")


def test_filters_and_exclusion_reasons():
    rows = [
        row("USDD"),
        row("OLD", status="Closed"),
        row("FUT", contractType="LinearFutures"),
        row("USDC", settleCoin="USDC"),
        row("PRE", status="PreLaunch"),
    ]
    artifact = build(rows, NOW, "c")
    assert {c["excluded_reason"] for c in artifact["body"]["contracts"]} == {
        "excluded_asset",
        "unknown_delist",
        "contractType",
        "settleCoin",
        "status",
    }
    assert all(c["pool"] is None for c in artifact["body"]["contracts"])


def test_priority_and_lifetime():
    rows = [
        row("1000ABC", launchTime="1"),
        row("ABC", status="Closed", deliveryTime="3000"),
        row("ABC", symbol="ZZUSDT", launchTime="1000"),
        row("ABC", symbol="AAUSDT", launchTime="2000"),
    ]
    contracts = build(rows, NOW, "c")["body"]["contracts"]
    assert active_contract(contracts, "ABC", 0) is None
    assert active_contract(contracts, "ABC", 500) == "1000ABCUSDT"
    assert active_contract(contracts, "ABC", 1000) == "ABCUSDT"
    assert active_contract(contracts, "ABC", 3000) == "ZZUSDT"


def test_deterministic_body_and_review_no_assignment():
    rows = [row("1000ABC"), row("ABC"), row("ABCD", status="Closed", deliveryTime="9000")]
    a = build(rows, NOW, "c")
    assert a == build(list(reversed(rows)), NOW, "c")
    assert a["sha256"] == digest(a["body"])
    with patch("richletsgo.research.d98.pool.assignment", side_effect=AssertionError):
        r = review(rows, NOW, "c")
    assert "pool" not in encode(r) and "JUDGMENT" not in encode(r)
    assert r["same_key_groups"]["ABC"] == ["1000ABCUSDT", "ABCUSDT"]
    assert r["similar_closed_trading"]
    for contract in a["body"]["contracts"]:
        assert contract["symbol"] not in markdown(a)


def test_cli_review_one_shot_and_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = Fake([{"list": [row()]}, {"list": []}])
    output = tmp_path / "한글" / "review.json"
    result = d98_pool.run(
        output, review_mode=True, client=client(fake), captured_at=NOW, commit="c"
    )
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert output.with_suffix(".requests.json").exists()
    with pytest.raises(ValueError, match="already exists"):
        d98_pool.run(output, review_mode=True, client=client(fake), captured_at=NOW, commit="c")
    assert len(fake.urls) == 2


def test_formal_guard_before_network(tmp_path):
    fake = Fake([])
    with pytest.raises(ValueError, match="await"):
        d98_pool.run(
            tmp_path / "pool.json",
            review_mode=False,
            client=client(fake),
            captured_at=NOW,
            commit="c",
        )
    assert not fake.urls


def test_failed_run_keeps_marker(tmp_path):
    fake = Fake([{"list": [], "nextPageCursor": "loop"}] * 2)
    output = tmp_path / "review.json"
    with pytest.raises(ValueError):
        d98_pool.run(output, review_mode=True, client=client(fake), captured_at=NOW, commit="c")
    assert output.with_suffix(".started.json").exists()
    with pytest.raises(FileExistsError):
        d98_pool.run(output, review_mode=True, client=client(fake), captured_at=NOW, commit="c")
    assert len(fake.urls) == 2


def test_formal_table_gate(monkeypatch):
    monkeypatch.setattr(tables, "TABLES_FROZEN", True)
    with patch.object(d98_pool, "git", side_effect=["", "a", "b"]):
        with pytest.raises(ValueError, match="not on origin/main"):
            d98_pool.formal_guard()
    with patch.object(d98_pool, "git", side_effect=["", "a", "a"]):
        d98_pool.formal_guard()


def test_closed_endpoint_mixed_status_is_recorded_then_filtered():
    fake = Fake(
        [
            {"list": []},
            {
                "list": [
                    row("OLD", status="Closed", deliveryTime="9000"),
                    row("PENDING", status="PendingOpen"),
                ]
            },
        ]
    )
    result = build(collect(client(fake)), NOW, "c")
    contracts = {c["symbol"]: c for c in result["body"]["contracts"]}
    assert contracts["PENDINGUSDT"]["excluded_reason"] == "status"
    assert contracts["PENDINGUSDT"]["pool"] is None
    assert contracts["OLDUSDT"]["pool"] in {"DESIGN", "JUDGMENT"}
