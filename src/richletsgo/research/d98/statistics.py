"""Streaming deterministic aggregation and fixed D98 progression criteria."""

import heapq
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from richletsgo.backtest.codec import digest

from .boundaries import epoch
from .models import GRID, D, calculated

BINS = tuple(
    D(x) for x in ("-1", "-0.5", "-0.2", "-0.1", "-0.05", "0", "0.05", "0.1", "0.2", "0.5", "1")
)


class Distribution:
    def __init__(self) -> None:
        self.n = 0
        self.missing = 0
        self.total = D(0)
        self.low: Decimal | None = None
        self.high: Decimal | None = None
        self.bins = [0] * (len(BINS) + 1)

    @calculated
    def add(self, value: Decimal | None) -> None:
        if value is None:
            self.missing += 1
            return
        self.n += 1
        self.total += value
        self.low = value if self.low is None else min(self.low, value)
        self.high = value if self.high is None else max(self.high, value)
        index = next((i for i, b in enumerate(BINS) if value <= b), len(BINS))
        self.bins[index] += 1

    @calculated
    def result(self) -> dict[str, Any]:
        return {
            "count": self.n,
            "NA": self.missing,
            "mean": self.total / self.n if self.n else None,
            "min": self.low,
            "max": self.high,
            "upper_inclusive_bins": list(BINS),
            "bin_counts": self.bins,
        }


def trim_key(row: dict[str, Any]) -> tuple[Decimal, int, tuple[int, ...]]:
    return (D(row["R"]), -row["t"], tuple(-ord(c) for c in row["symbol"]) + (1,))


class Aggregate:
    def __init__(self, valid_count: int) -> None:
        self.expected = valid_count
        self.trim_count = (valid_count + 99) // 100
        self.top: list[tuple[Decimal, int, tuple[int, ...]]] = []
        self.distribution = Distribution()
        self.years: dict[int, Distribution] = {y: Distribution() for y in range(2022, 2027)}
        self.technical: dict[str, Distribution] = {}
        self.forward = {str(n): Distribution() for n in (1, 3, 7)}

    def add(self, row: dict[str, Any]) -> None:
        value = D(row["R"])
        self.distribution.add(value)
        year = datetime.fromtimestamp(row["t"] / 1000, UTC).year
        self.years.setdefault(year, Distribution()).add(value)
        labels = [
            f"direction:{row['d']}",
            f"slippage:{D(row['slippage']).normalize()}",
            f"exit:{row['exit_reason']}",
            "before_2023_10" if row["t"] < epoch("2023-10-01") else "from_2023_10",
        ]
        for key in labels:
            self.technical.setdefault(key, Distribution()).add(value)
        for key, v in row["forward_returns"].items():
            self.forward[key].add(D(v) if v is not None else None)
        if self.trim_count:
            rank = trim_key(row)
            if len(self.top) < self.trim_count:
                heapq.heappush(self.top, rank)
            elif rank > self.top[0]:
                heapq.heapreplace(self.top, rank)

    @calculated
    def result(self, fired: int, invalid: dict[str, int]) -> dict[str, Any]:
        total = self.distribution.n
        if total != self.expected or fired != total + sum(invalid.values()):
            raise ValueError("signal counts disagree")
        undecidable = sum(invalid.values()) * 20 > fired
        mean = self.distribution.total / total if total else None
        remaining = total - self.trim_count
        trimmed = (
            (self.distribution.total - sum((k[0] for k in self.top), D(0))) / remaining
            if remaining
            else None
        )
        years = [v for v in self.years.values() if v.n >= 30]
        criteria = [
            total >= 300 and mean is not None and mean >= D("0.10"),
            trimmed is not None and trimmed > 0,
            len(years) >= 3 and sum(v.total > 0 for v in years) * 2 > len(years),
        ]
        return {
            "signals": fired,
            "valid": total,
            "invalid": sum(invalid.values()),
            "invalid_reasons": dict(sorted(invalid.items())),
            "undecidable": undecidable,
            "mean_R": mean,
            "trim_count": self.trim_count,
            "trimmed_mean_R": trimmed,
            "trim_order_sha256": digest(sorted(self.top, reverse=True)),
            "years": {str(y): v.result() for y, v in sorted(self.years.items())},
            "criteria_1_3": criteria,
            "candidate": not undecidable and all(criteria),
            "criterion_4_positive": not undecidable and mean is not None and mean > 0,
            "technical": {k: v.result() for k, v in sorted(self.technical.items())},
            "forward_returns": {k: v.result() for k, v in self.forward.items()},
        }


def verdict(results: dict[str, dict[str, Any]], eligible: int, insufficient: int) -> str:
    if set(results) != {c.key for c in GRID}:
        raise ValueError("all eight combinations required")
    if insufficient * 20 > eligible:
        return "STUDY_UNDECIDABLE"
    return (
        "GO"
        if any(r["candidate"] for r in results.values())
        and sum(r["criterion_4_positive"] for r in results.values()) >= 4
        else "NO_GO"
    )
