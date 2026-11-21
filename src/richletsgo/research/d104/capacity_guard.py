"""D104 capacity-only daily turnover collection. B is exclusive."""

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .inputs import CALENDAR, DAY, B, Session, eligible_sessions, number
from .verdict import minimum

STOCK = "D103-instruments-2026-11-10/stock.json"
SESSIONS = "D103-sessions.json"
PINS = {
    STOCK: "d90396f8590e33ea0c24e5114197b31a9e5cf007966da5d441deb73ed5b1da42",
    SESSIONS: "386ea41cb50792af7322d19223b05204baf796213986dc7fb3cb21bd98cfbd07",
}
TASK_HASH = "fcffc37ed64befac43121b5c386a5b2fda8e66f804c0cf264931052f25cc0842"
PAGE = 30
MAX_BYTES = 65536


def read_scope(reports: Path) -> tuple[dict[str, int], list[Session]]:
    def pinned(name: str) -> dict[str, Any]:
        raw = (reports / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != PINS[name]:
            raise ValueError("scope file hash mismatch")
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("invalid scope document")
        return obj

    instruments = pinned(STOCK)["result"]["list"]
    launches = {r["symbol"]: int(r["launchTime"]) for r in instruments if r["symbol"] in CALENDAR}
    if launches.keys() != CALENDAR.keys():
        raise ValueError("incomplete launch metadata")
    calendars = pinned(SESSIONS)["calendars"]
    spec = reports.parent / "tasks" / "M2-11-d104-tradfi-strategy.md"
    if hashlib.sha256(spec.read_bytes()).hexdigest() != TASK_HASH:
        raise ValueError("capacity contract changed; scope review required")
    sessions = [
        Session(sym, cal, a, b)
        for sym, cal in CALENDAR.items()
        for a, b, kind in calendars[cal]
        if kind == "regular" and launches[sym] <= a < b <= B
    ]
    return launches, sessions


def request_path(symbol: str, launch: int, start: int, end: int) -> str:
    if (
        symbol not in CALENDAR
        or not 0 < launch <= start < end <= B
        or start % DAY
        or end % DAY
        or end - start > PAGE * DAY
    ):
        raise ValueError("outside capacity scope")
    params = dict(
        category="linear",
        symbol=symbol,
        interval="D",
        start=str(start),
        end=str(end - 1),
        limit=str((end - start) // DAY),
    )
    return "/v5/market/kline?" + urlencode(params)


def _public_get(path: str) -> bytes:
    conn = HTTPSConnection("api.bybit.com", timeout=20)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        if response.status != 200:
            raise OSError("daily request failed")
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversize response")
        return raw
    finally:
        conn.close()


def page(
    symbol: str, launch: int, start: int, end: int, get: Callable[[str], bytes]
) -> dict[int, Decimal]:
    for _ in range(3):
        path = request_path(symbol, launch, start, end)
        try:
            raw = get(path)
        except (OSError, HTTPException):
            continue
        try:
            response = json.loads(raw)
            if response["retCode"] != 0:
                raise ValueError
            result = response["result"]
            if result["symbol"] != symbol or result["category"] != "linear":
                raise ValueError
            rows = result["list"]
            if not isinstance(rows, list):
                raise ValueError
            daily: dict[int, Decimal] = {}
            for row in rows:
                if not isinstance(row, list) or len(row) != 7:
                    raise ValueError
                if not isinstance(row[0], str) or not row[0].isascii() or not row[0].isdigit():
                    raise ValueError
                at = int(row[0])
                if at % DAY or not start <= at <= end - DAY:
                    raise ValueError
                if not isinstance(row[6], str) or not re.fullmatch(
                    r"[0-9]{1,40}(?:\.[0-9]{1,40})?", row[6]
                ):
                    raise ValueError
                value = Decimal(row[6])
                if not value.is_finite() or value < 0 or daily.setdefault(at, value) != value:
                    raise ValueError
        except (ValueError, TypeError, KeyError, InvalidOperation, UnicodeError):
            now = datetime.now(UTC).isoformat()
            raise ValueError(f"INVALID_INPUT {now} {symbol} [{start},{end})") from None
        if len(daily) == (end - start) // DAY:
            return daily
        if rows:
            break
    raise ValueError(f"INVALID_INPUT incomplete {symbol} [{start},{end})")


def collect(reports: Path, get: Callable[[str], bytes] = _public_get) -> dict[str, object]:
    launches, sessions = read_scope(reports)
    daily: dict[str, dict[int, Decimal]] = {}
    digest = hashlib.sha256()
    upper = dict.fromkeys(CALENDAR, 0)
    for sym in sorted(CALENDAR):
        launch = launches[sym]
        needed: set[int] = set()
        for s in sessions:
            first = s.start // DAY * DAY - 30 * DAY
            if s.symbol == sym and first >= launch:
                upper[sym] += 1
                needed.update(range(first, first + 30 * DAY, DAY))
        days = sorted(needed)
        values = daily[sym] = {}
        i = 0
        while i < len(days):
            j = i + 1
            while j < len(days) and j - i < PAGE and days[j] == days[j - 1] + DAY:
                j += 1
            values.update(page(sym, launch, days[i], days[j - 1] + DAY, get))
            i = j
        for at in days:
            line = json.dumps([sym, at, number(values[at])], separators=(",", ":"))
            digest.update((line + "\n").encode())
    eligible = dict.fromkeys(CALENDAR, 0)
    groups: dict[tuple[int, str], set[str]] = {}
    for session in eligible_sessions(sessions, launches, daily):
        eligible[session.symbol] += 1
        utc_day = session.start // DAY * DAY
        groups.setdefault((utc_day, session.calendar), set()).add(session.symbol)
    eligible_by_day: dict[int, int] = {}
    slot_capacity = 0
    calendar_days = []
    for (utc_day, calendar), symbols in sorted(groups.items()):
        count = len(symbols)
        slots = min(3, count)
        slot_capacity += slots
        eligible_by_day[utc_day] = eligible_by_day.get(utc_day, 0) + count
        calendar_days.append(
            dict(utc_day_ms=utc_day, calendar=calendar, eligible=count, slots=slots)
        )
    total = sum(eligible.values())
    return {
        "schema": "d104-capacity-v3",
        "task_sha256": TASK_HASH,
        "boundary_exclusive": B,
        "input_sha256": PINS,
        "daily_sha256": digest.hexdigest(),
        "daily_count": {s: len(v) for s, v in daily.items()},
        "upper": upper,
        "eligible": eligible,
        "multi_symbol_dates": sum(count >= 2 for count in eligible_by_day.values()),
        "E": total,
        "E_slot": slot_capacity,
        "calendar_days": calendar_days,
        "status": "SUFFICIENT" if total >= 150 else "INDETERMINATE",
        "N_min": minimum(slot_capacity) if total >= 150 else None,
    }
