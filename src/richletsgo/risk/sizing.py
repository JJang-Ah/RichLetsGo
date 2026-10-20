"""Entry sizing from stop distance (02 3장, P5) and the D62 liquidation approximation."""

from dataclasses import dataclass
from decimal import Decimal

from richletsgo.domain import InstrumentSpec, Price, Qty
from richletsgo.domain.values import finite_decimal

from .limits import RiskLimits
from .records import Basis, Direction, RejectionCode

AI_FACTOR_MIN = Decimal("0.5")  # D15 range, not a tunable risk value


def approx_liquidation_price(
    entry: Decimal, leverage: Decimal, mmr: Decimal, direction: Direction
) -> Decimal:
    """APPROXIMATION (02 3.1, D62): isolated margin, one-way, USDT linear.

    Ignores fees, funding, added margin and risk-tier changes. Use only to screen
    entries before sending; M3 re-checks against the exchange-reported price.
    """
    imr = Decimal(1) / leverage
    if direction is Direction.LONG:
        return entry * (1 - imr + mmr)
    return entry * (1 + imr - mmr)


def liquidation_buffer_ok(
    entry: Decimal, stop: Decimal, liquidation: Decimal, buffer: Decimal, direction: Direction
) -> bool:
    """02 3.1 inequality: buffer is a share of the entry-to-liquidation distance; equal passes."""
    if direction is Direction.LONG:
        return stop >= liquidation + buffer * (entry - liquidation)
    return stop <= liquidation - buffer * (liquidation - entry)


@dataclass(frozen=True, slots=True)
class SizingInput:
    equity: Decimal
    direction: Direction
    entry_price: Price
    stop_price: Price
    ai_factor: Decimal
    fee_rate: Decimal
    slippage: Decimal
    instrument: InstrumentSpec
    leverage: Decimal
    mmr: Decimal

    def __post_init__(self) -> None:
        if finite_decimal(self.equity, "equity") <= 0:
            raise ValueError("equity must be positive")
        if not AI_FACTOR_MIN <= finite_decimal(self.ai_factor, "ai_factor") <= 1:
            raise ValueError("ai_factor must be within 0.5..1.0 (D15)")
        for name in ("fee_rate", "slippage", "mmr"):
            value = finite_decimal(getattr(self, name), name)
            if not 0 <= value < 1:
                raise ValueError(f"{name} must be within [0, 1)")
        finite_decimal(self.leverage, "leverage")


@dataclass(frozen=True, slots=True)
class SizingResult:
    reason: RejectionCode | None
    qty: Qty | None
    aligned_stop: Decimal | None
    basis: Basis

    @property
    def ok(self) -> bool:
        return self.reason is None


def size_entry(inp: SizingInput, limits: RiskLimits) -> SizingResult:
    """Leverage only sets margin; quantity comes from allowed loss / stop distance."""
    entry = inp.entry_price.value
    basis: list[tuple[str, str]] = [
        ("equity", str(inp.equity)),
        ("entry_price", str(entry)),
        ("stop_price", str(inp.stop_price.value)),
        ("ai_factor", str(inp.ai_factor)),
        ("leverage", str(inp.leverage)),
    ]

    def fail(reason: RejectionCode) -> SizingResult:
        return SizingResult(reason, None, None, tuple(basis))

    max_leverage = min(limits.max_leverage, inp.instrument.max_leverage)
    if not 1 <= inp.leverage <= max_leverage:
        return fail(RejectionCode.LEVERAGE_OUT_OF_RANGE)

    # 1. Align the stop away from the entry so rounding never shortens the stop distance.
    if inp.direction is Direction.LONG:
        aligned = inp.instrument.round_price_down(inp.stop_price.value)
        valid_side = Decimal(0) < aligned < entry
    else:
        aligned = inp.instrument.round_price_up(inp.stop_price.value)
        valid_side = aligned > entry
    basis.append(("aligned_stop", str(aligned)))
    if not valid_side:
        return fail(RejectionCode.INVALID_STOP)

    # 2-3. Allowed loss and per-unit loss including entry/exit slippage and round-trip fees.
    allowed_loss = inp.equity * limits.risk_per_trade * inp.ai_factor
    per_unit = (entry - aligned).copy_abs() + (inp.slippage + inp.fee_rate) * (entry + aligned)
    basis += [("allowed_loss", str(allowed_loss)), ("stop_distance", str(per_unit))]

    # 4. Floor to the instrument step; never raise to the minimum.
    abs_qty = inp.instrument.round_qty_down(allowed_loss / per_unit)
    basis.append(("qty", str(abs_qty)))
    if abs_qty == 0:
        return fail(RejectionCode.BELOW_MIN_QTY)

    # 5. Liquidation screen (approximation).
    liquidation = approx_liquidation_price(entry, inp.leverage, inp.mmr, inp.direction)
    basis += [("mmr", str(inp.mmr)), ("liquidation_approx", str(liquidation))]
    beyond_entry = liquidation >= entry if inp.direction is Direction.LONG else liquidation <= entry
    if beyond_entry or not liquidation_buffer_ok(
        entry, aligned, liquidation, limits.liquidation_buffer_ratio, inp.direction
    ):
        return fail(RejectionCode.LIQUIDATION_TOO_CLOSE)

    basis.append(("margin", str(abs_qty * entry / inp.leverage)))
    signed = abs_qty if inp.direction is Direction.LONG else abs_qty.copy_negate()
    return SizingResult(None, Qty(signed), aligned, tuple(basis))
