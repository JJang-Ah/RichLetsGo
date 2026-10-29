"""D88 research profile loader. Required fields, paths relative to the TOML file."""

import tomllib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from richletsgo.domain import StrategyId, Symbol
from richletsgo.domain.values import finite_decimal
from richletsgo.oms.synchronizer import SyncPolicy
from richletsgo.portfolio.models import ClosePolicy
from richletsgo.risk.limits import RiskLimits


@dataclass(frozen=True, slots=True)
class ResearchProfile:
    limits: RiskLimits
    start_equity: Decimal
    taker_fee: Decimal
    maker_fee: Decimal
    slippage: Decimal

    def __post_init__(self) -> None:
        if finite_decimal(self.start_equity, "start_equity") <= 0:
            raise ValueError("positive initial equity required")
        for name in ("taker_fee", "maker_fee", "slippage"):
            if not 0 <= finite_decimal(getattr(self, name), name) < 1:
                raise ValueError(f"{name} must be a ratio in [0,1)")


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    profile: ResearchProfile
    close_policy: ClosePolicy
    sync_policy: SyncPolicy
    market_data_dir: Path
    results_dir: Path
    journal_dir: Path
    symbols: tuple[Symbol, ...]
    selection: dict[str, Any]  # M2-04 owns interpretation; preserve it, never use live risk.


def load_research_config(path: Path) -> ResearchConfig:
    path = path.resolve()
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if data["environment"] != "backtest":
        raise ValueError("research config requires environment=backtest")
    raw = data["backtest"]["research_risk"]

    def dec(key: str) -> Decimal:
        value = raw[key]
        if not isinstance(value, str):
            raise TypeError(f"{key} must be a decimal string")
        return Decimal(value)

    shares = raw["strategy_allocations"]
    if set(shares) != {s.value for s in StrategyId} or any(
        not isinstance(v, str) for v in shares.values()
    ):
        raise ValueError("allocations require Decimal strings for S1 and S2")
    cooldown = raw["cooldown_hours"]
    if type(cooldown) is not int:
        raise TypeError("cooldown_hours must be int")
    limits = RiskLimits(
        dec("risk_per_trade"),
        dec("daily_loss_limit_usdt"),
        dec("max_drawdown_ratio"),
        {s: Decimal(shares[s.value]) for s in StrategyId},
        raw["consecutive_loss_limit"],
        timedelta(hours=cooldown),
        dec("max_leverage"),
        dec("max_symbol_notional_usdt"),
        dec("max_account_notional_usdt"),
        dec("liquidation_buffer_ratio"),
        dec("slippage_buffer"),
        dec("resume_equity_tolerance_ratio"),
    )
    execution = data["backtest"]["execution"]

    def seconds(key: str) -> timedelta:
        value = execution[key]
        if type(value) is not int:
            raise TypeError(f"{key} must be int seconds")
        return timedelta(seconds=value)

    close = ClosePolicy(
        execution["max_close_attempts"],
        execution["max_protect_attempts"],
        seconds("retry_after_seconds"),
        execution["trigger_by"],
    )
    sync = SyncPolicy(
        seconds("history_window_seconds"),
        seconds("overlap_seconds"),
        seconds("unknown_alert_after_seconds"),
        seconds("cancel_retry_after_seconds"),
        execution["max_sync_rounds"],
    )
    paths = data["paths"]["backtest"]

    def directory(key: str) -> Path:
        target = Path(paths[key]).expanduser()
        return target.resolve() if target.is_absolute() else (path.parent / target).resolve()

    names = data["backtest"].get("symbols", ["BTCUSDT", "ETHUSDT"])
    symbols = tuple(Symbol(n) for n in names)
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("nonempty unique research symbols required")
    symbols = tuple(sorted(symbols, key=lambda s: s.value))
    return ResearchConfig(
        ResearchProfile(
            limits,
            dec("start_equity_usdt"),
            dec("taker_fee_rate"),
            dec("maker_fee_rate"),
            dec("slippage_rate"),
        ),
        close,
        sync,
        directory("market_data_dir"),
        directory("backtest_results_dir"),
        directory("journal_dir"),
        symbols,
        dict(data["backtest"]["selection"]),
    )
