import json
from urllib.parse import parse_qs, urlsplit

import pytest

from richletsgo.research.d104 import capacity_guard as g
from richletsgo.research.d104.inputs import BAR, CALENDAR, DAY, B, Session
from richletsgo.research.d104.replay import FundingPeriod, expected_settlements


def response(rows, symbol="MUUSDT"):
    return json.dumps(
        {"retCode": 0, "result": {"category": "linear", "symbol": symbol, "list": rows}}
    ).encode()


def row(at, value="20000000"):
    return [str(at), *(["never-publish"] * 5), value]


@pytest.mark.parametrize(
    "sym,start,end",
    [
        ("BTCUSDT", B - DAY, B),
        ("MUUSDT", B, B + DAY),
        ("MUUSDT", B - DAY + 1, B),
        ("MUUSDT", B - 31 * DAY, B),
        ("MUUSDT", 0, DAY),
    ],
)
def test_reject_before_transport(sym, start, end):
    with pytest.raises(ValueError):
        g.page(sym, DAY, start, end, lambda _: pytest.fail("sent"))


@pytest.mark.parametrize(
    "bad",
    [
        row(B),
        row(B - 2 * DAY),
        row(B - DAY + 1),
        row(B - DAY, "NaN"),
        row(B - DAY, "-1"),
        ["sensitive"],
    ],
)
def test_bad_response_stops_before_retry_and_redacts(bad):
    calls = []

    def get(path):
        calls.append(path)
        return response([row(B - DAY), bad])

    with pytest.raises(ValueError, match="INVALID_INPUT") as exc:
        g.page("MUUSDT", DAY, B - DAY, B, get)
    assert len(calls) == 1 and "never-publish" not in str(exc.value)


def test_empty_retry_and_missing_or_conflicting_rows():
    calls = []

    def get(path):
        calls.append(path)
        if len(calls) == 1:
            raise TimeoutError
        return response([] if len(calls) == 2 else [row(B - DAY)] * 2)

    assert len(g.page("MUUSDT", DAY, B - DAY, B, get)) == 1
    assert len(calls) == 3 and len(set(calls)) == 1
    for data in (
        response([]),
        response([row(B - DAY)]),
        response([row(B - DAY), row(B - DAY, "1")]),
        response([row(B - DAY)], "OTHER"),
    ):
        with pytest.raises(ValueError):
            g.page("MUUSDT", DAY, B - 2 * DAY, B, lambda _, raw=data: raw)


def test_synthetic_capacity_paging_projection_and_digest(monkeypatch, tmp_path):
    launch = B - 36 * DAY
    launches = dict.fromkeys(CALENDAR, launch)
    sessions = [
        Session(sym, cal, day, day + 24 * BAR)
        for sym, cal in CALENDAR.items()
        for day in range(B - 6 * DAY, B, DAY)
    ]
    monkeypatch.setattr(g, "read_scope", lambda _: (launches, sessions))
    calls = []

    def get(path):
        url = urlsplit(path)
        assert url.path == "/v5/market/kline"
        q = parse_qs(url.query)
        assert set(q) == {"category", "symbol", "interval", "start", "end", "limit"}
        start, end = int(q["start"][0]), int(q["end"][0]) + 1
        assert q["interval"] == ["D"] and q["category"] == ["linear"]
        assert launch <= start < end <= B and int(q["limit"][0]) == (end - start) // DAY
        calls.append(path)
        return response([row(at) for at in reversed(range(start, end, DAY))], q["symbol"][0])

    result = g.collect(tmp_path, get)
    assert len(calls) == 2 * len(CALENDAR)
    assert result["upper"] == result["eligible"] == dict.fromkeys(CALENDAR, 6)
    assert result["E"] == 30 and result["N_min"] is None
    assert "never-publish" not in json.dumps(result)
    assert g.collect(tmp_path, get) == result
    # A published calendar hash mismatch must block network access.
    monkeypatch.undo()
    source = tmp_path / g.STOCK
    source.parent.mkdir()
    source.write_bytes(b"{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        g.collect(tmp_path, lambda _: pytest.fail("sent"))


def test_historical_settlement_change_missing_period_and_overlap():
    s = Session("MUUSDT", "XNYS", DAY, DAY + 24 * BAR)
    change = s.start + 12 * BAR
    a = FundingPeriod(s.start, change, 8 * BAR, s.start)
    b = FundingPeriod(change, s.end, 4 * BAR, change)
    assert expected_settlements(s, (b, a)) == {
        s.start,
        s.start + 8 * BAR,
        change,
        change + 4 * BAR,
        change + 8 * BAR,
    }
    for periods in ((a,), (a, a, b)):
        with pytest.raises(ValueError, match="settlement"):
            expected_settlements(s, periods)


def test_us_regular_session_contains_1600_utc_settlement():
    hour = 12 * BAR
    session = Session("MUUSDT", "XNYS", DAY + 13 * hour + 6 * BAR, DAY + 20 * hour)
    assert expected_settlements(session, (FundingPeriod(0, B, 8 * hour, 0),)) == {DAY + 16 * hour}
