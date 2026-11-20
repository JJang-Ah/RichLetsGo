import json
from urllib.parse import parse_qs, urlsplit

import pytest

from richletsgo.research.d104 import stage1 as g
from richletsgo.research.d104.stage1_report import report


def test_synthetic_collect_freeze_then_offline_replay(tmp_path, monkeypatch):
    day = g.B - g.DAY
    start = day + 12 * g.BAR
    symbols = sorted(g.CALENDAR)[:4]
    sessions = [g.Session(s, g.CALENDAR[s], start, start + 12 * g.BAR) for s in symbols]
    launch = day - 30 * g.DAY
    monkeypatch.setattr(g, "scope", lambda _: (dict.fromkeys(symbols, launch), sessions))
    monkeypatch.setattr(
        g,
        "DAILY_HASH",
        g.sha(
            b"".join(
                g.packed([sym, t, "5000000"]) for sym in symbols for t in range(launch, day, g.DAY)
            )
        ),
    )
    settlement = start + 8 * g.BAR

    def get(path):
        url = urlsplit(path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        sym = q["symbol"]
        if "startTime" in q:
            assert int(q["startTime"]) == start and int(q["endTime"]) < g.B
            return g.packed(
                dict(
                    retCode=0,
                    result=dict(
                        category="linear",
                        list=[
                            dict(
                                symbol=sym, fundingRateTimestamp=str(settlement), fundingRate=".001"
                            )
                        ],
                    ),
                )
            )
        assert int(q["end"]) < g.B and q["category"] == "linear"
        if q["interval"] == "D":
            rows = [[str(t), *(["x"] * 5), "5000000"] for t in range(launch, day, g.DAY)]
        else:
            rows = []
            for i in range(12):
                prices = ["100", "101", "96", "100"] if i < 6 else ["102", "104", "101", "103"]
                row = [str(start + i * g.BAR), *prices]
                if url.path.endswith("/kline"):
                    row += ["123", "456"]
                rows.append(row)
        return g.packed(
            dict(retCode=0, result=dict(category="linear", symbol=sym, list=rows[::-1]))
        )

    folder = tmp_path / "campaign"
    g.collect(folder, tmp_path, get)
    source = g.sha(b"fixture")
    (folder / "sources").mkdir()
    (folder / "sources" / source).write_bytes(b"fixture")
    period = {"from": start, "until": sessions[0].end, "source": source}
    evidence = {
        s: dict(
            contracts=[
                dict(
                    period,
                    tick=".01",
                    step=".01",
                    min_qty="0",
                    min_value="0",
                    actions_halts="verified_clear",
                )
            ],
            schedule=[dict(period, interval=g.DAY, anchor=settlement)],
            settlement_marks={str(settlement): dict(price="102", source=source)},
        )
        for s in symbols
    }
    (folder / "evidence.json").write_bytes(g.packed(evidence))
    inputs, manifest = g.fixtures(folder, tmp_path)
    assert manifest["counts"] == manifest["expected"]
    result = report(inputs)
    assert result["N"] == 3 and result["verdict"] == "NO_GO"
    assert result["contention_by_symbol"][symbols[-1]] == 1
    assert result["costs"]["ALL"]["funding_cash"] < 0 and result["costs"]["ALL"]["fees"] > 0
    assert not result["cost_rejections"]
    del evidence[symbols[0]]["settlement_marks"][str(settlement)]
    (folder / "evidence.json").write_bytes(g.packed(evidence))
    inputs, _ = g.fixtures(folder, tmp_path)
    with pytest.raises(ValueError, match="missing settlement"):
        report(inputs)
    (folder / "trade.jsonl").write_bytes(b"")
    with pytest.raises((KeyError, ValueError)):
        g.fixtures(folder, tmp_path)


@pytest.mark.parametrize("kind", ["trade", "mark", "funding"])
def test_reject_exclusive_end_without_retry_or_echo(kind):
    s = g.Session("MUUSDT", "XNYS", g.B - 12 * g.BAR, g.B)
    calls = []

    def get(path):
        calls.append(path)
        item = (
            {"symbol": s.symbol, "fundingRateTimestamp": str(g.B), "fundingRate": ".123456789"}
            if kind == "funding"
            else [str(g.B), *([".123456789"] * (6 if kind == "trade" else 4))]
        )
        return json.dumps(
            {"retCode": 0, "result": {"category": "linear", "symbol": s.symbol, "list": [item]}}
        ).encode()

    with pytest.raises(ValueError, match="INVALID_INPUT") as exc:
        g.fetch_session(kind, s, get)
    assert len(calls) == 1 and ".123456789" not in str(exc.value)
