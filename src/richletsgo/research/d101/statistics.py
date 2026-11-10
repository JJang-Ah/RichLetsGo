"""Deterministic D101 progression criteria and technical summaries."""

import heapq
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from richletsgo.backtest.codec import digest
from richletsgo.research.d98.boundaries import DAY
from richletsgo.research.d98.models import D, calculated

from .models import GRID


class Distribution:
    def __init__(self) -> None:
        self.count = 0
        self.total = D(0)
        self.low: Decimal | None = None
        self.high: Decimal | None = None

    @calculated
    def add(self, value: Decimal) -> None:
        self.count += 1
        self.total += value
        self.low = value if self.low is None else min(self.low, value)
        self.high = value if self.high is None else max(self.high, value)

    @calculated
    def result(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "mean": self.total / self.count if self.count else None,
            "min": self.low,
            "max": self.high,
        }


def _symbol_tiebreak(symbol: str) -> tuple[int, ...]:
    return tuple(-ord(char) for char in symbol)


class Aggregate:
    """Streaming result accumulator; only top-1%, top-10 and year/month bins persist."""

    def __init__(self, fired: int, invalid: dict[str, int]) -> None:
        self.fired = fired
        self.invalid = dict(sorted(invalid.items()))
        self.invalid_count = sum(invalid.values())
        self.undecidable = self.invalid_count * 20 > fired if fired else False
        valid_count = fired - self.invalid_count
        if valid_count < 0:
            raise ValueError("invalid signal count exceeds fired signals")
        # The fired/invalid counts are known before the merged trade stream is
        # consumed, so reserve the final top-1% capacity from the start. Growing
        # this heap while streaming would lose earlier candidates when ceil(n/100)
        # increases.
        self.trim_count = (valid_count + 99) // 100
        self.expected_valid = valid_count
        self.count = 0
        self.total_r = D(0)
        self.top_trim: list[tuple[Decimal, int, tuple[int, ...]]] = []
        self.years: dict[int, Distribution] = {}
        self.months: dict[str, Distribution] = {}
        self.groups: dict[str, Distribution] = {}
        self.holding_days = Distribution()
        self.bundle_count = 0
        self.bundle_total = D(0)
        self._bundle_t: int | None = None
        self._bundle_sum = D(0)
        self._bundle_n = 0
        self.top_pnl: list[tuple[Decimal, int, tuple[int, ...], dict[str, Any]]] = []
        self.total_pnl = D(0)
        self.total_fees = D(0)
        self.total_funding = D(0)
        self.total_slippage = D(0)

    @calculated
    def _flush_bundle(self) -> None:
        if self._bundle_n:
            self.bundle_count += 1
            self.bundle_total += self._bundle_sum / self._bundle_n
            self._bundle_n = 0
            self._bundle_sum = D(0)

    @calculated
    def add(self, row: dict[str, Any]) -> None:
        value = D(row["R"])
        at = int(row["t"])
        if self._bundle_t is not None and at != self._bundle_t:
            self._flush_bundle()
        self._bundle_t = at
        self._bundle_n += 1
        self._bundle_sum += value
        self.count += 1
        self.total_r += value
        rank = (value, -at, _symbol_tiebreak(row["symbol"]))
        if len(self.top_trim) < self.trim_count:
            heapq.heappush(self.top_trim, rank)
        elif rank > self.top_trim[0]:
            heapq.heapreplace(self.top_trim, rank)

        moment = datetime.fromtimestamp(at / 1000, UTC)
        self.years.setdefault(moment.year, Distribution()).add(value)
        month = moment.strftime("%Y-%m")
        self.months.setdefault(month, Distribution()).add(value)
        labels = {
            "direction": "long" if row["d"] == 1 else "short",
            "liquidity": str(row["liquidity_bucket"]),
            "exit_reason": str(row["exit_reason"]),
            "btc_state": str(row["btc_state"]),
        }
        for kind, label in labels.items():
            self.groups.setdefault(f"{kind}:{label}", Distribution()).add(value)
        self.holding_days.add(D(row["holding_hours"]) / 24)
        self.total_pnl += D(row["pnl"])
        self.total_fees += D(row["fees"])
        self.total_funding += D(row["funding"])
        self.total_slippage += D(row["slippage_cost"])

        pnl_rank = (D(row["pnl"]), -at, _symbol_tiebreak(row["symbol"]), row)
        if len(self.top_pnl) < 10:
            heapq.heappush(self.top_pnl, pnl_rank)
        elif pnl_rank[:3] > self.top_pnl[0][:3]:
            heapq.heapreplace(self.top_pnl, pnl_rank)

    @calculated
    def result(self) -> dict[str, Any]:
        self._flush_bundle()
        if self.count != self.expected_valid or self.count + self.invalid_count != self.fired:
            raise ValueError("signal counts disagree")
        mean = self.total_r / self.count if self.count else None
        trim_sum = sum((rank[0] for rank in self.top_trim), D(0))
        remaining = self.count - self.trim_count
        trimmed_mean = (self.total_r - trim_sum) / remaining if remaining else None
        candidate_years = [value for value in self.years.values() if value.count >= 30]
        criteria = [
            self.count >= 300 and mean is not None and mean >= D("0.10"),
            trimmed_mean is not None and trimmed_mean > 0,
            len(candidate_years) >= 3
            and sum(value.total > 0 for value in candidate_years) * 2 > len(candidate_years),
            self.bundle_count > 0 and self.bundle_total / self.bundle_count > 0,
        ]
        costs: dict[str, Any] = {
            "fees": self.total_fees,
            "net_funding_paid": self.total_funding,
            "slippage": self.total_slippage,
        }
        total_cost = self.total_fees + self.total_funding + self.total_slippage
        costs["total"] = total_cost
        costs["shares"] = {
            name: value / total_cost if total_cost != 0 else None
            for name, value in (
                ("fees", self.total_fees),
                ("net_funding_paid", self.total_funding),
                ("slippage", self.total_slippage),
            )
        }
        top = [item[3] for item in sorted(self.top_pnl, key=lambda item: item[:3], reverse=True)]
        technical = {
            kind: {
                label: self.groups[f"{kind}:{label}"].result()
                for label in sorted(
                    key[len(kind) + 1 :] for key in self.groups if key.startswith(kind + ":")
                )
            }
            for kind in ("direction", "liquidity", "exit_reason", "btc_state")
        }
        return {
            "signals": self.fired,
            "valid": self.count,
            "invalid": self.invalid_count,
            "invalid_reasons": self.invalid,
            "undecidable": self.undecidable,
            "mean_R": mean,
            "trim_count": self.trim_count,
            "trimmed_mean_R": trimmed_mean,
            "trim_order_sha256": digest(sorted(self.top_trim, reverse=True)),
            "years": {str(year): self.years[year].result() for year in sorted(self.years)},
            "months": {month: self.months[month].result() for month in sorted(self.months)},
            "technical": technical,
            "holding_days": self.holding_days.result(),
            "entry_date_bundle_mean_R": (
                self.bundle_total / self.bundle_count if self.bundle_count else None
            ),
            "entry_date_bundle_count": self.bundle_count,
            "criteria_1_4": criteria,
            "candidate": not self.undecidable and all(criteria),
            "mean_R_positive": not self.undecidable and mean is not None and mean > 0,
            "total_pnl": self.total_pnl,
            "top_10_pnl": top,
            "top_10_pnl_total": sum((D(row["pnl"]) for row in top), D(0)),
            "top_10_pnl_share_of_total": (
                sum((D(row["pnl"]) for row in top), D(0)) / self.total_pnl
                if self.total_pnl != 0
                else None
            ),
            "costs": costs,
        }


def daily_holding_distribution(
    events: list[tuple[int, int]], *, start: int, end: int
) -> dict[str, Any]:
    """Distribution of each UTC date's peak overlapping open positions."""
    events.sort(key=lambda event: (event[0], event[1]))  # close before open at a shared instant
    counts: Counter[str] = Counter()
    cursor = 0
    open_positions = 0
    for day in range(start, end, DAY):
        day_end = day + DAY
        while cursor < len(events) and events[cursor][0] == day:
            _, delta = events[cursor]
            open_positions += delta
            cursor += 1
        peak = open_positions
        while cursor < len(events) and events[cursor][0] < day_end:
            _, delta = events[cursor]
            open_positions += delta
            peak = max(peak, open_positions)
            cursor += 1
        counts[str(peak)] += 1
    return {
        "method": "daily peak of intervals [entry, exit)",
        "counts": dict(sorted(counts.items())),
    }


def verdict(combinations: dict[str, dict[str, Any]], eligible: int, insufficient: int) -> str:
    if set(combinations) != {combo.key for combo in GRID}:
        raise ValueError("all four combinations required")
    if eligible and insufficient * 20 > eligible:
        return "STUDY_UNDECIDABLE"
    candidates = any(result["candidate"] for result in combinations.values())
    positive = sum(result["mean_R_positive"] for result in combinations.values())
    return "GO" if candidates and positive >= 2 else "NO_GO"
