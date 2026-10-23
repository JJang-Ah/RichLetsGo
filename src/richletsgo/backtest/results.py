"""Auditable trade attribution and deterministic SQLite artifacts."""

import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from richletsgo.domain import OrderPurpose, StrategyId, Symbol
from richletsgo.storage.contracts import FillStatus, UnitOfWork

from .codec import digest, encode, primitive

ZERO = Decimal(0)


@dataclass(slots=True)
class Trade:
    episode_id: str
    strategy: StrategyId
    symbol: Symbol
    direction: str
    opened_at: datetime
    initial_stop: Decimal
    allowed_loss: Decimal
    entry_qty: Decimal = ZERO
    entry_notional: Decimal = ZERO
    exit_qty: Decimal = ZERO
    exit_notional: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    gross_pnl: Decimal = ZERO
    closed_at: datetime | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> Decimal:
        return self.entry_qty - self.exit_qty

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.fees - self.funding

    def record(self) -> dict[str, Any]:
        result = primitive(self)
        result.update(
            entry_price=primitive(self.entry_notional / self.entry_qty),
            exit_price=primitive(self.exit_notional / self.exit_qty) if self.exit_qty else None,
            net_pnl=primitive(self.net_pnl),
            r_multiple=primitive(self.net_pnl / self.allowed_loss),
            remaining=primitive(self.remaining),
        )
        return dict(result)


class TradeLedger:
    def __init__(self) -> None:
        self.trades: list[Trade] = []
        self.active: dict[tuple[StrategyId, Symbol], Trade] = {}
        self.seen: set[str] = set()
        self.exit_reasons: dict[str, str] = {}

    def ingest(self, uow: UnitOfWork) -> None:
        entries = {key: ep for ep in uow.episodes.all() for key in ep.entry_order_keys}
        decisions = {d.order_key: d for d in uow.decisions.all() if d.order_key}
        for fill in uow.fills.all():
            event = fill.event
            if event.exec_id in self.seen:
                continue
            if (
                fill.status is not FillStatus.BOOKED
                or fill.order_key is None
                or fill.strategy is None
            ):
                raise ValueError("normal backtest cannot attribute an execution")
            order = uow.orders.get(fill.order_key)
            assert order is not None
            key = (fill.strategy, event.symbol)
            qty = abs(event.qty.value)
            if order.purpose is OrderPurpose.ENTRY:
                episode = entries[order.key]
                trade = self.active.get(key)
                if trade is None:
                    assert order.attached_stop is not None
                    basis = dict(decisions[order.key].basis)
                    trade = Trade(
                        episode.episode_id,
                        fill.strategy,
                        event.symbol,
                        episode.direction.value,
                        event.exec_time,
                        order.attached_stop.stop_loss.value,
                        Decimal(basis["allowed_loss"]),
                    )
                    self.trades.append(trade)
                    self.active[key] = trade
                if trade.episode_id != episode.episode_id:
                    raise ValueError("overlapping episodes")
                trade.entry_qty += qty
                trade.entry_notional += qty * event.price.value
            else:
                trade = self.active[key]
                if qty > trade.remaining:
                    raise ValueError("exit exceeds attributed trade")
                sign = 1 if trade.direction == "LONG" else -1
                trade.gross_pnl += (
                    qty * (event.price.value - trade.entry_notional / trade.entry_qty) * sign
                )
                trade.exit_qty += qty
                trade.exit_notional += qty * event.price.value
                if order.purpose in (OrderPurpose.ATTACHED_SL, OrderPurpose.PROTECT):
                    reason = (
                        "trailing_stop"
                        if order.trigger_price is not None
                        and order.trigger_price.value != trade.initial_stop
                        else "stop_loss"
                    )
                else:
                    reason = self.exit_reasons.get(trade.episode_id, "protective_close")
                if reason not in trade.reasons:
                    trade.reasons.append(reason)
                if trade.remaining == 0:
                    trade.closed_at = event.exec_time
                    del self.active[key]
            trade.fees += event.fee
            self.seen.add(event.exec_id)

    def funding(self, symbol: Symbol, cost: Decimal) -> None:
        trades = [t for t in self.active.values() if t.symbol is symbol]
        total = sum((t.remaining for t in trades), ZERO)
        if not total and cost:
            raise ValueError("funding without a position")
        left = cost
        for index, trade in enumerate(trades):
            part = left if index == len(trades) - 1 else cost * trade.remaining / total
            trade.funding += part
            left -= part


@dataclass(frozen=True, slots=True)
class BacktestResult:
    status: str
    issues: tuple[Any, ...]
    trades: tuple[dict[str, Any], ...]
    orders: tuple[Any, ...]
    executions: tuple[Any, ...]
    decisions: tuple[Any, ...]
    equity: tuple[dict[str, Any], ...]
    pauses: tuple[dict[str, Any], ...]
    trace: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]
    remaining_positions: tuple[Any, ...]

    @property
    def result_hash(self) -> str:
        content = primitive(self)
        content["metadata"].pop("executed_at", None)
        return digest(content)

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (self.result_hash + ".sqlite3")
        if target.exists():
            return target
        fd, temporary = tempfile.mkstemp(prefix="result-", suffix=".sqlite3", dir=directory)
        os.close(fd)
        temp = Path(temporary)
        try:
            with closing(sqlite3.connect(temp)) as db, db:
                db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
                metadata = dict(
                    self.metadata,
                    status=self.status,
                    result_hash=self.result_hash,
                    remaining_positions=self.remaining_positions,
                )
                db.executemany(
                    "INSERT INTO metadata VALUES (?,?)",
                    [(k, encode(v)) for k, v in metadata.items()],
                )
                for name in (
                    "issues",
                    "trades",
                    "orders",
                    "executions",
                    "decisions",
                    "equity",
                    "pauses",
                    "trace",
                ):
                    # Identifiers are this fixed internal list, never caller input.
                    db.execute(
                        f"CREATE TABLE {name} (sequence INTEGER PRIMARY KEY, data_json TEXT NOT NULL)"
                    )
                    db.executemany(
                        f"INSERT INTO {name} VALUES (?,?)",
                        [(i, encode(row)) for i, row in enumerate(getattr(self, name))],
                    )
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return target
