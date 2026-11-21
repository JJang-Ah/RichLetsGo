import json
from urllib.parse import parse_qs, urlsplit

import pytest

from richletsgo.research.d104 import stage1 as g
from richletsgo.research.d104.stage1_report import report


def test_synthetic_collect_freeze_then_offline_replay(tmp_path, monkeypatch):
    day = g.B - g.DAY
    start = day + 16 * 3_600_000 - 8 * g.BAR
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
                if i == 8 and url.path.endswith("/mark-price-kline"):
                    prices = ["107", "109", "101", "105"]
                row = [str(start + i * g.BAR), *prices]
                if url.path.endswith("/kline"):
                    row += ["123", "456"]
                rows.append(row)
        return g.packed(
            dict(retCode=0, result=dict(category="linear", symbol=sym, list=rows[::-1]))
        )

    folder = tmp_path / "campaign"
    folder.mkdir()
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
        )
        for s in symbols
    }
    (folder / "evidence.json").write_bytes(g.packed(evidence))
    pin = g.sha((folder / "evidence.json").read_bytes())

    def forbidden(_):
        pytest.fail("network access before evidence verification")

    with pytest.raises(ValueError, match="published evidence hash"):
        g.collect(folder, tmp_path, "0" * 64, forbidden)
    bad_schedule = evidence[symbols[0]]["schedule"][0]
    bad_schedule["source"] = ""
    invalid = g.packed(evidence)
    (folder / "evidence.json").write_bytes(invalid)
    with pytest.raises(ValueError, match="schedule evidence source"):
        g.collect(folder, tmp_path, g.sha(invalid), forbidden)
    assert not (folder / "collect-started").exists()
    bad_schedule["source"] = source
    (folder / "evidence.json").write_bytes(g.packed(evidence))
    (folder / "sources" / source).write_bytes(b"modified")
    with pytest.raises(ValueError, match="source mismatch"):
        g.collect(folder, tmp_path, pin, forbidden)
    (folder / "sources" / source).write_bytes(b"fixture")
    g.collect(folder, tmp_path, pin, get)
    inputs, manifest = g.fixtures(folder, tmp_path, pin)
    assert all(f.settlements[settlement] == (g.D(".001"), g.D(107)) for f in inputs)
    assert manifest["counts"] == manifest["expected"]
    result = report(inputs)
    assert result["N"] == 3 and result["verdict"] == "NO_GO"
    assert result["contention_by_symbol"][symbols[-1]] == 1
    assert result["costs"]["ALL"]["funding_cash"] < 0 and result["costs"]["ALL"]["fees"] > 0
    assert not result["cost_rejections"]
    assert result["costs"]["ALL"]["funding_cash"] == -3 * g.D("3.28") * g.D(".001") * 107
    with pytest.raises(ValueError, match="published evidence hash"):
        g.fixtures(folder, tmp_path, "0" * 64)
    mark = folder / "mark.jsonl"
    mark.write_bytes(
        b"".join(
            line
            for line in mark.read_bytes().splitlines(keepends=True)
            if json.loads(line)[:2] != [symbols[-1], settlement]
        )
    )
    with pytest.raises(ValueError, match="INVALID_INPUT.*mark candle"):
        g.fixtures(folder, tmp_path, pin)
    assert not (folder / "run-started").exists()


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


def test_cli_report_error_preserves_once_marker_and_writes_invalid(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("stage1_cli", root / "tools" / "d104_stage1.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "git", lambda *args: str(root) if "--show-toplevel" in args else "")
    reg = g.packed({})
    registration = tmp_path / "reg.json"
    registration.write_bytes(reg)
    monkeypatch.setattr(g, "registration", lambda *_: {})
    monkeypatch.setattr(g, "fixtures", lambda *_: ([], {}))

    def fail(_):
        raise ArithmeticError("synthetic report error")

    monkeypatch.setattr(cli, "report", fail)
    manifest = g.packed({"registration_sha256": g.sha(reg)})
    (tmp_path / "manifest.json").write_bytes(manifest)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tool",
            "run",
            "--registration",
            str(registration),
            "--folder",
            str(tmp_path),
            "--manifest-sha256",
            g.sha(manifest),
            "--evidence-sha256",
            "1" * 64,
        ],
    )
    cli.main()
    assert json.loads((tmp_path / "result.json").read_bytes()) == {
        "verdict": "INVALID_INPUT",
        "reason": "ArithmeticError: synthetic report error",
    }
    with pytest.raises(FileExistsError):
        cli.main()
