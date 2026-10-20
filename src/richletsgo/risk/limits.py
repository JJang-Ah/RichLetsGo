"""Risk limit values. Every field is required; there are no defaults anywhere (Q13)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from types import MappingProxyType

from richletsgo.domain import StrategyId
from richletsgo.domain.values import finite_decimal


def _ratio(value: Decimal, name: str, *, allow_zero: bool, allow_one: bool) -> None:
    finite_decimal(value, name)
    low_ok = value >= 0 if allow_zero else value > 0
    high_ok = value <= 1 if allow_one else value < 1
    if not (low_ok and high_ok):
        raise ValueError(f"{name} is outside its allowed ratio range")


def _positive(value: Decimal, name: str) -> None:
    if finite_decimal(value, name) <= 0:
        raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """02 risk policy values. Test values in tests are not operating recommendations."""

    risk_per_trade: Decimal
    daily_loss_limit_usdt: Decimal
    max_drawdown_ratio: Decimal
    strategy_allocations: Mapping[StrategyId, Decimal]
    consecutive_loss_limit: int
    cooldown: timedelta
    max_leverage: Decimal
    max_symbol_notional_usdt: Decimal
    max_account_notional_usdt: Decimal
    liquidation_buffer_ratio: Decimal
    slippage_buffer: Decimal
    resume_equity_tolerance_ratio: Decimal

    def __post_init__(self) -> None:
        _ratio(self.risk_per_trade, "risk_per_trade", allow_zero=False, allow_one=False)
        _positive(self.daily_loss_limit_usdt, "daily_loss_limit_usdt")
        _ratio(self.max_drawdown_ratio, "max_drawdown_ratio", allow_zero=False, allow_one=False)
        if not isinstance(self.strategy_allocations, Mapping):
            raise TypeError("strategy_allocations must be a mapping")
        if set(self.strategy_allocations) != set(StrategyId):
            raise ValueError("strategy_allocations must name every strategy exactly")
        for strategy, share in self.strategy_allocations.items():
            _ratio(share, f"allocation {strategy}", allow_zero=True, allow_one=True)
        if sum(self.strategy_allocations.values(), Decimal(0)) > 1:
            raise ValueError("strategy allocations must sum to at most 1")
        object.__setattr__(
            self, "strategy_allocations", MappingProxyType(dict(self.strategy_allocations))
        )
        if type(self.consecutive_loss_limit) is not int or self.consecutive_loss_limit < 1:
            raise ValueError("consecutive_loss_limit must be an int of at least 1")
        if not isinstance(self.cooldown, timedelta) or self.cooldown <= timedelta(0):
            raise ValueError("cooldown must be a positive timedelta")
        if finite_decimal(self.max_leverage, "max_leverage") < 1:
            raise ValueError("max_leverage must be at least 1")
        _positive(self.max_symbol_notional_usdt, "max_symbol_notional_usdt")
        _positive(self.max_account_notional_usdt, "max_account_notional_usdt")
        _ratio(
            self.liquidation_buffer_ratio,
            "liquidation_buffer_ratio",
            allow_zero=True,
            allow_one=False,
        )
        _ratio(self.slippage_buffer, "slippage_buffer", allow_zero=True, allow_one=False)
        _ratio(
            self.resume_equity_tolerance_ratio,
            "resume_equity_tolerance_ratio",
            allow_zero=True,
            allow_one=False,
        )

    def strategy_daily_limit(self, strategy: StrategyId) -> Decimal:
        """L4 limit. An allocation of zero disables new entries for that strategy."""
        return self.daily_loss_limit_usdt * self.strategy_allocations[strategy]
