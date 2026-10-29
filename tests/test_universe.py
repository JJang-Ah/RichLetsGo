from datetime import UTC, datetime

import pytest

from richletsgo.backtest.codec import digest
from richletsgo.marketdata.universe import MetadataTransport, select


def test_selection_filters_ties_and_ignores_prices() -> None:
    names = ["BTC", "ETH", "USDC", "PAXG", "1000X", "OLD", "NEW", "A", "B", "C", "D"]
    rows = [
        {
            "symbol": n + "USDT",
            "baseCoin": n,
            "quoteCoin": "USDT",
            "status": "Trading",
            "contractType": "LinearPerpetual",
            "launchTime": "1640995199999",
        }
        for n in names
    ]
    rows[6]["launchTime"] = "1640995200000"
    rows[5]["status"] = "Settling"
    ticks = [{"symbol": r["symbol"], "turnover24h": "100", "lastPrice": "999"} for r in rows]
    result = select(rows, ticks, datetime(2026, 1, 1, tzinfo=UTC))
    assert result["body"]["selected"] == ["AUSDT", "BUSDT", "CUSDT", "DUSDT"]
    assert result["sha256"] == digest(result["body"])
    for t in ticks:
        t["lastPrice"] = "1"
    assert (
        select(list(reversed(rows)), list(reversed(ticks)), datetime(2026, 1, 1, tzinfo=UTC))
        == result
    )


@pytest.mark.parametrize(
    "path", ["/v5/market/kline", "/v5/market/funding/history", "/v5/order/create"]
)
def test_metadata_transport_rejects_other_endpoints(path: str) -> None:
    class Never:
        def get(self, url: str, timeout: float) -> tuple[int, bytes]:
            raise AssertionError("transport must not be reached")

    with pytest.raises(ValueError, match="metadata"):
        MetadataTransport(Never()).get("https://api.bybit.com" + path, 1)


def test_all_instrument_pages_and_one_ticker_snapshot():
    import json
    from urllib.parse import urlsplit

    from richletsgo.marketdata.bybit_public import BybitPublicClient, RetryPolicy
    from richletsgo.marketdata.universe import collect

    class Pages:
        calls = []

        def get(self, url, timeout):
            self.calls.append(url)
            if urlsplit(url).path.endswith("tickers"):
                result = {
                    "list": [
                        {"symbol": s + "USDT", "turnover24h": str(i)} for i, s in enumerate("ABCD")
                    ]
                }
            else:
                last = "cursor=" in url
                result = {
                    "nextPageCursor": "" if last else "page2",
                    "list": [
                        {
                            "symbol": s + "USDT",
                            "baseCoin": s,
                            "quoteCoin": "USDT",
                            "status": "Trading",
                            "contractType": "LinearPerpetual",
                            "launchTime": "1",
                        }
                        for s in ("CD" if last else "AB")
                    ],
                }
            return 200, json.dumps({"retCode": 0, "result": result}).encode()

    transport = Pages()
    client = BybitPublicClient(
        "https://api.bybit.com",
        transport=MetadataTransport(transport),
        policy=RetryPolicy(0, 1, 0, 1),
        sleep=lambda _: None,
    )
    result = collect(client, datetime(2026, 1, 1, tzinfo=UTC))
    assert result["body"]["selected"] == ["DUSDT", "CUSDT", "BUSDT", "AUSDT"]
    assert len(transport.calls) == 3


def test_once_marker_refuses_before_network(tmp_path, monkeypatch):
    import sys

    from richletsgo.marketdata import universe

    output = tmp_path / "selection.json"
    output.with_suffix(".started.json").write_text("{}")
    monkeypatch.setattr(sys, "argv", ["select", "--output", str(output)])

    def never():
        raise AssertionError("must not start another collection")

    monkeypatch.setattr(universe, "UrllibTransport", never)
    with pytest.raises(FileExistsError):
        universe.main()
