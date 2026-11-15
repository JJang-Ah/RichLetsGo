"""Settlement-time allocation; all records share the caller's transaction."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from richletsgo.domain import StrategyId, Symbol
from richletsgo.domain.clock import require_utc, trading_day_start
from richletsgo.domain.values import finite_decimal
from richletsgo.storage.contracts import FillStatus, UnitOfWork

from .models import Action, ActionKind


class FundingSnapshotMissing(ValueError):
    pass


def snapshot_key(at: datetime, symbol: Symbol) -> str:
    return f"position_snapshot:{require_utc(at).isoformat()}:{symbol.value}"


def position_snapshot(uow: UnitOfWork, at: datetime, symbol: Symbol) -> dict[str, str]:
    """Freeze positions just before at, reversing already-booked later fill deltas."""
    key = snapshot_key(at, symbol)
    saved = uow.recovery.get(key)
    if saved:
        return {str(s): str(q) for s, q in json.loads(saved).items()}
    if any(
        f.event.symbol is symbol
        and f.event.exec_time < at
        and f.status is FillStatus.PENDING_ATTRIBUTION
        for f in uow.fills.all()
    ):
        raise FundingSnapshotMissing("pre-boundary fill attribution pending")
    if any(o.symbol is symbol and o.unbooked_qty for o in uow.orders.all()):
        raise FundingSnapshotMissing("unbooked exchange quantity pending")
    quantities = {s.value: uow.portfolio.book(s, symbol).qty for s in StrategyId}
    for fill in uow.fills.all():
        if (
            fill.event.symbol is not symbol
            or fill.status is FillStatus.PENDING_ATTRIBUTION
            or fill.event.exec_time < at
        ):
            continue
        raw = uow.recovery.get("position_delta:" + fill.event.exec_id)
        if raw is None:
            raise FundingSnapshotMissing("post-boundary fill has no allocation delta")
        for strategy, delta in json.loads(raw).items():
            quantities[strategy] -= Decimal(delta)
    record = {s: str(q) for s, q in quantities.items()}
    uow.recovery.put(key, json.dumps(record, sort_keys=True))
    return record


def allocate(
    uow: UnitOfWork, funding_id: str, symbol: Symbol, amount: Decimal, settled_at: datetime
) -> None:
    require_utc(settled_at)
    finite_decimal(amount, "funding amount")
    if not funding_id:
        raise ValueError("funding_id required")
    if uow.portfolio.funding_seen(funding_id):
        return
    raw = uow.recovery.get(snapshot_key(settled_at, symbol))
    if raw is None:
        raise FundingSnapshotMissing("settlement position snapshot required")
    positions = {StrategyId(s): abs(Decimal(q)) for s, q in json.loads(raw).items()}
    total = sum(positions.values(), Decimal(0))
    day = (
        trading_day_start(settled_at - timedelta(microseconds=1))
        if settled_at == trading_day_start(settled_at)
        else trading_day_start(settled_at)
    )
    record = {
        "id": funding_id,
        "symbol": symbol.value,
        "amount": str(amount),
        "settled_at": settled_at.isoformat(),
        "day": day.isoformat(),
    }
    left = amount
    held = [s for s in StrategyId if positions.get(s, Decimal(0))]
    for index, strategy in enumerate(held):
        share = left if index == len(held) - 1 else amount * positions[strategy] / total
        left -= share
        book = uow.portfolio.book(strategy, symbol)
        uow.portfolio.put_book(replace(book, funding=book.funding + share))
    if not total and amount:
        uow.recovery.put("unallocated_funding:" + funding_id, json.dumps(record, sort_keys=True))
        uow.portfolio.emit(
            Action(
                "funding:" + funding_id + ":unallocated",
                ActionKind.ALERT,
                StrategyId.S1,
                symbol,
                detail="미배분 펀딩: " + funding_id,
            )
        )
    uow.recovery.put("funding_record:" + funding_id, json.dumps(record, sort_keys=True))
    uow.portfolio.mark_funding(funding_id)
