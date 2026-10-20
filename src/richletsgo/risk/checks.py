"""L1-L7 checks (02 2장). L4-L7 produce pauses; L2-L3 reject single entries."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from richletsgo.domain import Qty, StrategyId, Symbol
from richletsgo.domain.clock import require_utc
from richletsgo.domain.values import finite_decimal
from richletsgo.oms.exposure import ExposureView

from .equity import EquityTracker
from .limits import RiskLimits
from .pause import Pause, PauseKind, PauseState
from .records import Rejection, RejectionCode


@dataclass(frozen=True, slots=True)
class StrategyRiskInput:
    """Per-strategy figures supplied by the strategy book (M1-04)."""

    daily_pnl: Decimal
    consecutive_losses: int
    last_loss_at: datetime | None

    def __post_init__(self) -> None:
        finite_decimal(self.daily_pnl, "daily_pnl")
        if type(self.consecutive_losses) is not int or self.consecutive_losses < 0:
            raise ValueError("consecutive_losses must be a nonnegative int")
        if self.last_loss_at is not None:
            require_utc(self.last_loss_at)
        if self.consecutive_losses > 0 and self.last_loss_at is None:
            raise ValueError("a loss streak needs last_loss_at")


def evaluate_pauses(
    limits: RiskLimits,
    tracker: EquityTracker,
    strategies: Mapping[StrategyId, StrategyRiskInput],
    now: datetime,
) -> tuple[Pause, ...]:
    """Pauses the current figures call for. Reaching a limit (>=) triggers it (P4)."""
    require_utc(now)
    next_day = tracker.day_start + timedelta(days=1)
    pauses: list[Pause] = []
    if tracker.daily_loss >= limits.daily_loss_limit_usdt:
        pauses.append(Pause(PauseKind.ENTRY_PAUSED_DAILY, "L5", now, until=next_day))
    if tracker.drawdown_ratio >= limits.max_drawdown_ratio:
        pauses.append(Pause(PauseKind.HALTED, "L6", now))
    for strategy in StrategyId:
        figures = strategies.get(strategy)
        disabled = limits.strategy_daily_limit(strategy) == 0
        if disabled or (
            figures is not None
            and max(Decimal(0), -figures.daily_pnl) >= limits.strategy_daily_limit(strategy)
        ):
            pauses.append(
                Pause(PauseKind.STRATEGY_PAUSED, "L4", now, strategy=strategy, until=next_day)
            )
        if figures is not None and figures.consecutive_losses >= limits.consecutive_loss_limit:
            assert figures.last_loss_at is not None
            resume_at = figures.last_loss_at + limits.cooldown
            if now < resume_at:
                pauses.append(
                    Pause(PauseKind.STRATEGY_PAUSED, "L7", now, strategy=strategy, until=resume_at)
                )
    return tuple(pauses)


@dataclass(frozen=True, slots=True)
class LimitCheck:
    reducing: bool
    rejections: tuple[Rejection, ...]

    @property
    def allowed(self) -> bool:
        return not self.rejections


def is_reducing(held: Decimal, order_qty: Qty) -> bool:
    """Strictly reduces the strategy's own holding without crossing zero."""
    qty = order_qty.value
    return held != 0 and (held > 0) != (qty > 0) and qty.copy_abs() <= held.copy_abs()


def check_limits(
    *,
    limits: RiskLimits,
    view: ExposureView,
    pauses: PauseState,
    strategy: StrategyId,
    symbol: Symbol,
    held_for_strategy: Decimal,
    order_qty: Qty,
    notional_price: Decimal,
    leverage: Decimal,
) -> LimitCheck:
    """Reducing orders are exempt from L1-L3 and every pause (02, REVIEW finding 1).

    `view` must be the unfiltered account view so pending fills and every contract count.
    """
    if order_qty.value == 0:
        raise ValueError("order quantity must be nonzero")
    if is_reducing(held_for_strategy, order_qty):
        return LimitCheck(True, ())
    rejections = [
        Rejection(RejectionCode.PAUSED, f"{p.kind.value}:{p.reason}")
        for p in pauses.blocking(strategy, symbol)
    ]
    if not 1 <= leverage <= limits.max_leverage:
        rejections.append(Rejection(RejectionCode.LEVERAGE_OUT_OF_RANGE, f"leverage {leverage}"))
    added = order_qty.value.copy_abs() * notional_price
    symbol_now = sum(
        (row.total_notional for row in view.symbols if row.symbol is symbol), Decimal(0)
    )
    if symbol_now + added > limits.max_symbol_notional_usdt:
        rejections.append(Rejection(RejectionCode.L2_SYMBOL_NOTIONAL, f"{symbol_now} + {added}"))
    if view.total_notional + added > limits.max_account_notional_usdt:
        rejections.append(
            Rejection(RejectionCode.L3_ACCOUNT_NOTIONAL, f"{view.total_notional} + {added}")
        )
    return LimitCheck(False, tuple(rejections))
