"""Frozen design-only scope, Bybit projection and offline integrity checks."""

import hashlib
import json
from collections.abc import Callable, Iterable
from decimal import Decimal as D
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .capacity_guard import PINS, STOCK, TASK_HASH, _public_get, page, read_scope
from .diagnostics import Attempt, CollectionFailure
from .execution import Terms
from .inputs import BAR, CALENDAR, DAY, B, Candle, Session, number
from .replay import Fixture, FundingPeriod, expected_settlements

CUTOVER = 1786118400000
EIGHT_HOURS = 28_800_000
NOTICE_HASH = "89ce07ba511175c89633312ee50d284e636f82b68539331f1d2e640fedffd34d"
HALTS_HASH = "13a8c54ba5e88eebd6dfff8571563adbe5963f1de924400e4d91c50335debeaa"
MODEL_LABEL = "2026-11-10 주문 규격 소급 적용, 8/7 16:00 UTC 이전 펀딩 시각은 API 기록 사용, 중단은 D103 조사·공지 사건과 5분봉 완전성 검사로 갈음"
CAP_HASH = "f1138f734d9c63cdff061ee2e89484098dffe862514138d593604c988e66e6f4"
DAILY_HASH = "79d5a596e575d63567eba9a5cde7d665032c4f8f4b71ef1e9f4a9f3c020426fc"
KINDS = ("daily", "funding", "mark", "trade")
PATHS = {"trade": "kline", "mark": "mark-price-kline", "funding": "funding/history"}
Row = list[Any]


def packed(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def windows(launches: dict[str, int], sessions: list[Session]) -> Iterable[tuple[str, int, int]]:
    for sym in sorted(launches):
        days: set[int] = set()
        for s in sessions:
            midnight = s.start // DAY * DAY
            if s.symbol == sym and midnight - 30 * DAY >= launches[sym]:
                days.update(range(midnight - 30 * DAY, midnight, DAY))
        ordered = sorted(days)
        i = 0
        while i < len(ordered):
            j = i + 1
            while j < len(ordered) and j - i < 30 and ordered[j] == ordered[j - 1] + DAY:
                j += 1
            yield sym, ordered[i], ordered[j - 1] + DAY
            i = j


def scope(reports: Path) -> tuple[dict[str, int], list[Session]]:
    if sha((reports / "D104-capacity.json").read_bytes()) != CAP_HASH:
        raise ValueError("capacity result changed")
    launches, sessions = read_scope(reports)
    return launches, sorted(sessions, key=lambda s: (s.symbol, s.start))


def registration(code: str, reports: Path) -> dict[str, Any]:
    launches, sessions = scope(reports)
    plan = [[s.symbol, s.calendar, s.start, s.end] for s in sessions]
    return dict(
        schema="D104-stage1-v4",
        funding_cutover_ms=CUTOVER,
        funding_notice_sha256=NOTICE_HASH,
        halts_sha256=HALTS_HASH,
        order_terms_model=MODEL_LABEL,
        settlement_mark="same_timestamp_mark_5m_open",
        evidence_pin="pre_collection_evidence_json_sha256",
        code=code,
        B_exclusive=B,
        spec_sha256=TASK_HASH,
        scope_files=PINS,
        capacity_sha256=CAP_HASH,
        daily_sha256=DAILY_HASH,
        E=491,
        E_slot=232,
        N_min=100,
        session_count=len(plan),
        session_plan_sha256=sha(packed(plan)),
        daily_plan_sha256=sha(packed(list(windows(launches, sessions)))),
        symbols=sorted(CALENDAR),
    )


def dec(value: object) -> D:
    if (
        not isinstance(value, str)
        or len(value) > 80
        or not value.lstrip("-").replace(".", "", 1).isdigit()
    ):
        raise ValueError("invalid decimal field")
    n = D(value)
    if not n.is_finite():
        raise ValueError("nonfinite")
    return n


def parse_rows(kind: str, s: Session, raw: bytes, attempt: Attempt | None = None) -> list[Row]:
    attempt = attempt or Attempt()
    result = attempt.result(raw)
    if result["category"] != "linear" or not isinstance(result["list"], list):
        raise ValueError("invalid envelope")
    if kind != "funding" and result["symbol"] != s.symbol:
        raise ValueError("invalid symbol")
    unique: dict[int, Row] = {}
    for item in result["list"]:
        if kind == "funding":
            if item["symbol"] != s.symbol:
                raise ValueError("foreign funding symbol")
            timestamp = item["fundingRateTimestamp"]
            if not isinstance(timestamp, str) or not timestamp.isascii() or not timestamp.isdigit():
                raise ValueError("invalid funding timestamp")
            at = int(timestamp)
            if at in unique:
                raise ValueError("duplicate funding timestamp")
            vals = [number(dec(item["fundingRate"]))]
        else:
            if not isinstance(item, list) or len(item) != (7 if kind == "trade" else 5):
                raise ValueError("bad kline")
            at = int(item[0])
            prices = [dec(x) for x in item[1:]]
            Candle(s.symbol, at, *prices[:4])
            if any(n < 0 for n in prices[4:]):
                raise ValueError("negative volume")
            vals = list(map(number, prices))
        if at % BAR or not s.start <= at <= s.end - BAR:
            raise ValueError("outside request")
        row = [s.symbol, at, *vals]
        if unique.setdefault(at, row) != row:
            raise ValueError("conflict")
    if kind == "funding" and list(unique) not in (sorted(unique), sorted(unique, reverse=True)):
        raise ValueError("unordered funding timestamps")
    if kind != "funding" and set(unique) != set(range(s.start, s.end, BAR)):
        attempt.missing(range(s.start, s.end, BAR), unique)
        raise ValueError("incomplete session")
    return [unique[t] for t in sorted(unique)]


def fetch_session(kind: str, s: Session, get: Callable[[str], bytes]) -> list[Row]:
    # Session is constructed only from the hash-pinned regular-calendar plan.
    span = (s.end - s.start) // BAR
    if kind not in PATHS or not 0 < s.start < s.end <= B or span > 200:
        raise ValueError("invalid stage1 request")
    q = dict(category="linear", symbol=s.symbol)
    if kind == "funding":
        q.update(startTime=str(s.start), endTime=str(s.end - 1), limit="200")
    else:
        q.update(interval="5", start=str(s.start), end=str(s.end - 1), limit=str(span))
    path = "/v5/market/" + PATHS[kind] + "?" + urlencode(q)
    attempts: list[Attempt] = []
    for _ in range(3):
        attempt = Attempt()
        attempts.append(attempt)
        try:
            raw = attempt.request(get, path)
            if raw is None:
                continue
            return parse_rows(kind, s, raw, attempt)
        except (ValueError, KeyError, TypeError, ArithmeticError, IndexError):
            break
    raise CollectionFailure(
        kind, s.symbol, s.start, s.end, None if kind == "funding" else span, attempts
    )


def collect(
    folder: Path, reports: Path, evidence_sha256: str, get: Callable[[str], bytes] = _public_get
) -> None:
    launches, sessions = scope(reports)
    check_evidence(folder, reports, launches, sessions, evidence_sha256)
    if {p.name for p in folder.iterdir()} != {"evidence.json", "sources"}:
        raise ValueError("expected unused evidence-only campaign folder")
    with (folder / "collect-started").open("xb") as marker:
        marker.write(packed(evidence_sha256))
    for kind in KINDS:
        with (folder / (kind + ".jsonl")).open("xb") as f:
            if kind == "daily":
                for sym, start, end in windows(launches, sessions):
                    daily = page(sym, launches[sym], start, end, get)
                    for at in sorted(daily):
                        f.write(packed([sym, at, number(daily[at])]))
            else:
                for session in sessions:
                    for row in fetch_session(kind, session, get):
                        f.write(packed(row))
        if kind == "daily" and sha((folder / "daily.jsonl").read_bytes()) != DAILY_HASH:
            raise ValueError("INVALID_INPUT daily revision")


def read_candles(rows: dict[tuple[str, int], Row], session: Session) -> tuple[Candle, ...]:
    return tuple(
        Candle(session.symbol, t, *(dec(n) for n in rows.pop((session.symbol, t))[2:6]))
        for t in range(session.start, session.end, BAR)
    )


SessionEvidence = tuple[Terms, tuple[FundingPeriod, ...]]


def check_evidence(
    folder: Path, reports: Path, launches: dict[str, int], sessions: list[Session], published: str
) -> tuple[dict[str, str], dict[Session, SessionEvidence]]:
    raw = (folder / "evidence.json").read_bytes()
    if sha(raw) != published:
        raise ValueError("published evidence hash mismatch")
    evidence = json.loads(raw)
    if (
        set(evidence) != {"schedule", "halts_source", "additional_events"}
        or evidence["schedule"]
        != dict(source=NOTICE_HASH, from_ms=CUTOVER, interval_ms=EIGHT_HOURS)
        or evidence["halts_source"] != HALTS_HASH
    ):
        raise ValueError("invalid funding schedule or halt source")
    snapshot = (reports / STOCK).read_bytes()
    if sha(snapshot) != PINS[STOCK]:
        raise ValueError("order terms snapshot hash mismatch")
    terms_by_symbol = {}
    for row in json.loads(snapshot)["result"]["list"]:
        sym = row["symbol"]
        if sym in launches:
            lot = row["lotSizeFilter"]
            if sym in terms_by_symbol:
                raise ValueError("duplicate order terms")
            terms_by_symbol[sym] = Terms(
                D(".0025"),
                dec(row["priceFilter"]["tickSize"]),
                dec(lot["qtyStep"]),
                dec(lot["minOrderQty"]),
                dec(lot["minNotionalValue"]),
            )
    if terms_by_symbol.keys() != launches.keys():
        raise ValueError("missing snapshot order terms")
    files = {"evidence.json": published}

    def source(digest: str) -> bytes:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid evidence source")
        name = "sources/" + digest
        data = (folder / name).read_bytes()
        if sha(data) != digest:
            raise ValueError("evidence source mismatch")
        files[name] = digest
        return data

    source(NOTICE_HASH)
    events = json.loads(source(HALTS_HASH))["halts"]
    for event in evidence["additional_events"]:
        source(event["source"])
        events.setdefault(event["symbol"], []).append(event)
    for records in events.values():
        for event in records:
            if (
                type(event["start"]) is not int
                or type(event["end"]) is not int
                or not 0 <= event["start"] < event["end"]
            ):
                raise ValueError("invalid announced halt")
    history = (FundingPeriod(CUTOVER, B, EIGHT_HOURS, CUTOVER),)
    plan: dict[Session, SessionEvidence] = {}
    for s in sessions:
        if any(
            event["start"] < s.end and s.start < event["end"] for event in events.get(s.symbol, [])
        ):
            raise ValueError("announced event overlaps registered session")
        plan[s] = (terms_by_symbol[s.symbol], history)
    return files, plan


def fixtures(
    folder: Path, reports: Path, evidence_sha256: str
) -> tuple[list[Fixture], dict[str, Any]]:
    launches, sessions = scope(reports)
    files, plan = check_evidence(folder, reports, launches, sessions, evidence_sha256)
    if (folder / "collect-started").read_bytes() != packed(evidence_sha256):
        raise ValueError("pre-collection evidence mismatch")
    rows: dict[str, dict[tuple[str, int], Row]] = {}
    counts: dict[str, dict[str, int]] = {}
    for kind in KINDS:
        filename = kind + ".jsonl"
        raw = (folder / filename).read_bytes()
        files[filename] = sha(raw)
        if kind == "daily" and files[filename] != DAILY_HASH:
            raise ValueError("daily revision")
        by = rows[kind] = {}
        counts[kind] = dict.fromkeys(launches, 0)
        for line in raw.splitlines(keepends=True):
            r = json.loads(line)
            sym, at, *_ = r
            if (
                packed(r) != line
                or type(at) is not int
                or not 0 <= at < B
                or sym not in launches
                or len(r) != {"trade": 8, "mark": 6, "daily": 3, "funding": 3}[kind]
                or (sym, at) in by
            ):
                raise ValueError("invalid normalized input")
            if kind == "trade" and any(dec(n) < 0 for n in r[6:]):
                raise ValueError("negative volume")
            if by and next(reversed(by)) >= (sym, at):
                raise ValueError("unordered rows")
            by[sym, at] = r
            counts[kind][sym] += 1
    days: dict[str, dict[int, D]] = {s: {} for s in launches}
    for (sym, t), r in rows["daily"].items():
        days[sym][t] = dec(r[2])
    expected = {k: dict.fromkeys(launches, 0) for k in KINDS}
    for sym, start, end in windows(launches, sessions):
        expected["daily"][sym] += (end - start) // DAY
    result = []
    for s in sessions:
        sym = s.symbol
        terms, history = plan[s]
        observed = {t for t in range(s.start, s.end, BAR) if (sym, t) in rows["funding"]}
        times = expected_settlements(s, history, CUTOVER, observed)
        if times != observed:
            raise ValueError("INVALID_INPUT funding schedule mismatch")
        settlements: dict[int, tuple[D, D]] = {}
        expected["funding"][sym] += len(times)
        for kind in ("trade", "mark"):
            expected[kind][sym] += (s.end - s.start) // BAR
        for t in times:
            key = (sym, t)
            if (
                not s.start <= t < s.end
                or t % BAR
                or key not in rows["mark"]
                or key not in rows["funding"]
            ):
                raise ValueError(
                    "INVALID_INPUT: missing or invalid settlement boundary, rate or mark candle"
                )
            settlements[t] = (dec(rows["funding"].pop(key)[2]), dec(rows["mark"][key][2]))

        result.append(
            Fixture(
                s,
                read_candles(rows["trade"], s),
                read_candles(rows["mark"], s),
                launches[sym],
                days[sym],
                terms,
                settlements,
                history,
                CUTOVER,
            )
        )
    if counts != expected:
        raise ValueError("unexpected evidence or row counts")
    if any(rows[k] for k in ("funding", "mark", "trade")):
        raise ValueError("unexpected rows")
    return result, dict(
        schema="D104-stage1-data-v1",
        files=files,
        counts=counts,
        expected=expected,
        missing=0,
        corrections=[],
        data_hash=sha(packed(files)),
    )
