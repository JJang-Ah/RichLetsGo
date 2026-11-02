"""Shared D98 collection boundaries and immutable pool identity."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from richletsgo.backtest.codec import digest

HOUR = 3_600_000
DAY = 24 * HOUR


def epoch(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)


START = epoch("2021-11-01")
END = epoch("2026-10-09")
STUDY_START = epoch("2022-01-01")
STUDY_END = epoch("2026-10-01")
BTC_START = epoch("2020-11-07")


@dataclass(frozen=True)
class Window:
    start: int
    end: int
    delist: int | None

    @property
    def empty(self) -> bool:
        return self.start >= self.end

    def contains(self, time: int, kind: str) -> bool:
        if self.empty:
            return False
        if kind == "bars":
            return time % HOUR == 0 and self.start <= time and time + HOUR <= self.end
        if kind == "funding":
            return self.start - DAY <= time < END and (self.delist is None or time <= self.delist)
        raise ValueError("unknown series")


def window(contract: dict[str, Any]) -> Window:
    d = contract["delist"]
    return Window(
        max(contract["launch"] // HOUR * HOUR, START), min(END, d if d is not None else END), d
    )


def load_pool(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if digest(value["body"]) != value["sha256"] or value["body"]["rule_version"] != "D98-pool-v1":
        raise ValueError("invalid pool digest/version")
    return dict(value)


def design_contracts(pool: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if digest(pool["body"]) != pool["sha256"]:
        raise ValueError("invalid pool digest")
    return {
        c["symbol"]: c
        for c in pool["body"]["contracts"]
        if c["pool"] == "DESIGN" and c["excluded_reason"] is None
    }
