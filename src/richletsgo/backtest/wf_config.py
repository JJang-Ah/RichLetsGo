"""Explicit walk-forward inputs; no implicit parameter grids or compute budget."""

import calendar
import tomllib
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import product
from pathlib import Path
from typing import Any

from richletsgo.domain.clock import require_utc
from richletsgo.marketdata.models import Interval
from richletsgo.strategy.candidates import required_bars
from richletsgo.strategy.models import S1AParams, S1BParams, S2AParams, S2BParams, S2PParams

from .codec import digest
from .config import ResearchConfig, load_research_config

S1 = S1AParams | S1BParams
S2 = S2AParams | S2BParams | S2PParams


def utc(value: str) -> datetime:
    return require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def months(at: datetime, count: int) -> datetime:
    year, month = divmod(at.year * 12 + at.month - 1 + count, 12)
    return at.replace(
        year=year, month=month + 1, day=min(at.day, calendar.monthrange(year, month + 1)[1])
    )


def parameter_key(param: S1 | S2 | None) -> tuple[Decimal, ...]:
    return () if param is None else tuple(Decimal(getattr(param, f.name)) for f in fields(param))


@dataclass(frozen=True)
class Window:
    index: int
    train_start: datetime
    train_end: datetime
    end: datetime


@dataclass(frozen=True)
class WalkConfig:
    research: ResearchConfig
    start: datetime
    end: datetime
    train_months: int
    test_months: int
    workers: int
    parallel_efficiency: Decimal
    per_run_seconds_12m: Decimal
    max_compute_hours: Decimal
    reestimate_after: int
    measurement_source: str
    grids: dict[str, tuple[tuple[S1, S2 | None], ...]]
    drawdown_floor: Decimal
    minimum_trades: dict[str, int]
    max_invalid_ratio: Decimal
    pass_criteria: dict[str, Decimal] | None
    preregistration: dict[str, str] | None = None
    strategy_minimum: dict[str, Decimal] | None = None

    @property
    def windows(self) -> tuple[Window, ...]:
        result = []
        i = 0
        while True:
            start = months(self.start, i * self.test_months)
            training_end = months(self.start, i * self.test_months + self.train_months)
            end = months(self.start, i * self.test_months + self.train_months + self.test_months)
            if end > self.end:
                break
            result.append(Window(i, start, training_end, end))
            i += 1
        return tuple(result)

    @property
    def warmup_start(self) -> datetime:
        span = max(
            max(
                timedelta(days=required_bars(a)),
                timedelta(hours=required_bars(b) * (4 if isinstance(b, S2PParams) else 1))
                if b is not None
                else timedelta(0),
            )
            for pairs in self.grids.values()
            for a, b in pairs
        )
        return Interval.DAY.floor(self.start - span)


def load_walk_config(path: Path) -> WalkConfig:
    research = load_research_config(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))["backtest"]
    raw = data["walkforward"]

    def integer(key: str) -> int:
        v = raw[key]
        if type(v) is not int or v <= 0:
            raise ValueError(f"{key} must be positive int")
        return v

    def decimal(key: str) -> Decimal:
        if not isinstance(raw[key], str):
            raise TypeError(f"{key} must be a Decimal string")
        v = Decimal(raw[key])
        if not v.is_finite() or v <= 0:
            raise ValueError(f"invalid {key}")
        return v

    classes = {
        "S1A": S1AParams,
        "S1B": S1BParams,
        "S2A": S2AParams,
        "S2B": S2BParams,
        "S2P": S2PParams,
    }
    parsed: dict[str, list[Any]] = {}
    if not data["grid"] or set(data["grid"]) - set(classes):
        raise ValueError("unknown/empty candidate grid")
    for name, cls in classes.items():
        if name not in data["grid"]:
            continue
        candidates = []
        for row in data["grid"][name]:
            if set(row) != {f.name for f in fields(cls)}:
                raise ValueError(f"invalid {name} fields")
            values = {}
            for f in fields(cls):
                v = row[f.name]
                if f.type is Decimal:
                    if not isinstance(v, str):
                        raise TypeError(f"{f.name} must be a Decimal string")
                    v = Decimal(v)
                elif type(v) is not int:
                    raise TypeError(f"{f.name} must be int")
                values[f.name] = v
            candidates.append(cls(**values))
        if not candidates or len({digest(p) for p in candidates}) != len(candidates):
            raise ValueError("grids must be nonempty and unique")
        parsed[name] = sorted(candidates, key=parameter_key)
    selection = research.selection
    cap = selection["max_grid_points"]
    if type(cap) is not int or not 1 <= cap <= 24:
        raise ValueError("D91 max_grid_points must be 1..24")
    grids: dict[str, tuple[tuple[S1, S2 | None], ...]] = {}
    for a, b in product(
        (n for n in parsed if n.startswith("S1")), (n for n in parsed if n.startswith("S2"))
    ):
        pairs = tuple(product(parsed[a], parsed[b]))
        if len(pairs) > cap:
            raise ValueError("D91 joint grid exceeds cap")
        grids[a + "-" + b] = pairs
    if not any(n.startswith("S2") for n in parsed) and set(parsed) == {"S1B"}:
        if len(parsed["S1B"]) > cap:
            raise ValueError("D91 grid exceeds cap")
        grids["S1B"] = tuple((a, None) for a in parsed["S1B"])
    if not grids:
        raise ValueError("both strategy grids required, or S1B only")
    positive_score = selection.get("require_positive_score", False)
    if type(positive_score) is not bool:
        raise TypeError("require_positive_score must be bool")
    start, end = utc(raw["start"]), utc(raw["end"])
    if not Interval.HOUR.aligned(start) or not Interval.HOUR.aligned(end) or end <= start:
        raise ValueError("hour aligned nonempty range required")
    if any(
        not isinstance(selection[k], str)
        for k in ("drawdown_floor_usdt", "max_invalid_window_ratio")
    ):
        raise TypeError("selection ratios and money must be Decimal strings")
    floor = Decimal(selection["drawdown_floor_usdt"])
    ratio = Decimal(selection["max_invalid_window_ratio"])
    minimum = dict(selection["min_training_trades"])
    if not floor.is_finite() or floor <= 0 or not ratio.is_finite() or not 0 <= ratio <= 1:
        raise ValueError("invalid selection limits")
    if set(minimum) != ({"S1"} if set(grids) == {"S1B"} else {"S1", "S2"}) or any(
        type(v) is not int or v < 0 for v in minimum.values()
    ):
        raise ValueError("minimum trades require S1 and S2 nonnegative ints")
    criteria = dict(data["pass_criteria"]) if "pass_criteria" in data else None
    strategy_minimum = (
        criteria.pop("min_strategy_net_pnl_usdt", None) if criteria is not None else None
    )
    if strategy_minimum is not None:
        if (
            not isinstance(strategy_minimum, dict)
            or not strategy_minimum
            or set(strategy_minimum) - set(minimum)
            or any(not isinstance(v, str) for v in strategy_minimum.values())
        ):
            raise ValueError("invalid strategy pass criteria")
        strategy_minimum = {k: Decimal(v) for k, v in strategy_minimum.items()}
        if any(not v.is_finite() for v in strategy_minimum.values()):
            raise ValueError("nonfinite strategy pass criteria")
    if criteria is not None and any(not isinstance(v, str) for v in criteria.values()):
        raise TypeError("pass criteria must be Decimal strings")
    criteria = {k: Decimal(v) for k, v in criteria.items()} if criteria is not None else None
    if criteria is not None and (
        set(criteria) != {"min_net_pnl_usdt", "max_drawdown_ratio"}
        or any(not v.is_finite() for v in criteria.values())
        or not 0 < criteria["max_drawdown_ratio"] < 1
    ):
        raise ValueError("invalid pass criteria")
    config = WalkConfig(
        research,
        start,
        end,
        integer("train_months"),
        integer("test_months"),
        integer("workers"),
        decimal("parallel_efficiency"),
        decimal("per_run_seconds_12m"),
        decimal("max_compute_hours"),
        integer("reestimate_after"),
        str(raw["measurement_source"]),
        grids,
        floor,
        minimum,
        ratio,
        criteria,
        data.get("preregistration"),
        strategy_minimum,
    )
    if config.parallel_efficiency > 1 or not config.windows or not config.measurement_source:
        raise ValueError("invalid efficiency/windows/measurement source")
    if config.preregistration is not None:
        registration = config.preregistration
        if set(registration) != {"id", "sha256"} or not registration["id"]:
            raise ValueError("preregistration requires id and sha256")
        if len(registration["sha256"]) != 64 or any(
            c not in "0123456789abcdef" for c in registration["sha256"]
        ):
            raise ValueError("preregistration SHA-256 required")
    return config
