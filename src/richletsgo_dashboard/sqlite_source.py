"""Dashboard read model over the Trader's SQLite file (M5-01, D105).

Opens a fresh `mode=ro` connection per snapshot, so the dashboard can never write and
never holds a lock across requests; WAL lets it read while the Trader commits. Rows are
decoded with the storage codec (allow-listed domain types only) and mapped to display
records. Figures the Trader does not persist yet (equity history, limit usage, marks)
stay None and render as "-" rather than as invented numbers.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from richletsgo.portfolio.models import StrategyBook
from richletsgo.risk.pause import PauseKind, PauseState
from richletsgo.risk.records import DecisionRecord
from richletsgo.storage.codec import EPOCH, decode
from richletsgo.storage.contracts import FillRecord
from richletsgo.storage.sqlite import open_readonly

from .source import (
    Decision,
    Fill,
    LogEntry,
    Order,
    Overview,
    Position,
    Snapshot,
    SourceUnavailable,
)

PURPOSES = {"E": "진입", "X": "청산", "P": "보호", "ATTACHED_SL": "손절"}
ROWS = 200  # newest rows per screen; older history belongs to reports, not the live view

# Most severe account-wide pause wins the headline; the rest become reasons.
SEVERITY = (
    (PauseKind.HALTED, "HALTED"),
    (PauseKind.ENTRY_PAUSED_SYSTEM, "ENTRY_PAUSED_SYSTEM"),
    (PauseKind.ENTRY_PAUSED_DAILY, "ENTRY_PAUSED_RISK"),
    (PauseKind.ENTRY_PAUSED_MANUAL, "ENTRY_PAUSED_MANUAL"),
)


def _side(qty: Decimal) -> str:
    return "Buy" if qty > 0 else "Sell"


class SqliteSource:
    def __init__(self, path: Path, environment: str, *, ai_mode: str = "disabled") -> None:
        self.path, self.environment, self.ai_mode = path, environment, ai_mode

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise SourceUnavailable(f"database file not found: {self.path}")
        try:
            connection = open_readonly(self.path)
        except sqlite3.Error as error:
            raise SourceUnavailable("database cannot be opened read-only") from error
        with closing(connection):
            meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("environment") != self.environment:
                raise SourceUnavailable("database environment does not match the dashboard")
            yield connection

    def snapshot(self) -> Snapshot:
        with self._connect() as c:
            account = str(c.execute("SELECT value FROM meta WHERE key='account_id'").fetchone()[0])
            rows = self._rows(c, account)
        orders: list[Any] = rows["orders"]
        fills: list[FillRecord] = rows["fills"]
        books: list[StrategyBook] = rows["books"]
        decisions: list[DecisionRecord] = rows["decisions"]
        pauses: PauseState = rows["pauses"]
        state, reasons = self._pause(pauses)
        overview = Overview(
            environment=self.environment,
            account_id=account,
            pause_state=state,
            pause_reasons=reasons,
            equity=None,
            today_pnl=None,
            drawdown=None,
            limits=(),
            last_closed_bar=None,
            last_fill=max((f.event.exec_time for f in fills), default=None),
            key_expires_in_days=None,
            ai_mode=self.ai_mode,
        )
        return Snapshot(
            overview=overview,
            positions=[self._position(b) for b in books if b.qty != 0],
            orders=[self._order(o) for o in orders],
            fills=[self._fill(f) for f in fills],
            logs=sorted(
                self._pause_logs(pauses) + rows["commands"], key=lambda e: e.at, reverse=True
            ),
            decisions=[self._decision(d) for d in decisions],
        )

    def _rows(self, c: sqlite3.Connection, account: str) -> dict[str, Any]:
        def newest(table: str) -> list[Any]:
            return [
                decode(payload)
                for (payload,) in c.execute(
                    f"SELECT payload FROM {table} WHERE account_id=? "  # noqa: S608 fixed names
                    "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                    (account, ROWS),
                )
            ]

        pause_row = c.execute(
            "SELECT payload FROM risk_states WHERE account_id=? AND key='pauses'", (account,)
        ).fetchone()
        commands = [
            self._command(source, state, payload, created)
            for source, state, payload, created in c.execute(
                "SELECT source, state, payload, created_at FROM commands WHERE account_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (account, ROWS),
            )
        ]
        return {
            "orders": newest("orders"),
            "fills": newest("fills"),
            "decisions": newest("decisions"),
            "books": [
                decode(p)
                for (p,) in c.execute(
                    "SELECT payload FROM books WHERE account_id=? ORDER BY key", (account,)
                )
            ],
            "pauses": PauseState.from_json(json.loads(pause_row[0])) if pause_row else PauseState(),
            "commands": commands,
        }

    @staticmethod
    def _pause(pauses: PauseState) -> tuple[str, tuple[str, ...]]:
        active = pauses.active()
        account_wide = [p for p in active if p.strategy is None and p.symbol is None]
        state = "ACTIVE"
        for kind, label in SEVERITY:
            if any(p.kind is kind for p in account_wide):
                state = label
                break
        return state, tuple(p.reason for p in active)

    @staticmethod
    def _pause_logs(pauses: PauseState) -> list[LogEntry]:
        return [
            LogEntry(p.since, "pause", "system", f"{p.kind.value}: {p.reason}")
            for p in pauses.active()
        ]

    @staticmethod
    def _command(source: str, state: str, payload: str, created_ms: int) -> LogEntry:
        try:
            record = json.loads(payload)
            name = str(record.get("command") or record.get("name") or "command")
        except (ValueError, AttributeError):
            name = "command"
        at = EPOCH + timedelta(milliseconds=created_ms)
        return LogEntry(at, "command", source, f"{name} ({state})")

    @staticmethod
    def _position(book: StrategyBook) -> Position:
        return Position(
            strategy=book.strategy.value,
            symbol=book.symbol.value,
            side=_side(book.qty),
            quantity=abs(book.qty),
            entry_price=book.avg_price,
            mark_price=None,
            stop_price=book.stop_price.value if book.stop_price else None,
            exchange_stop_quantity=sum((s.qty for s in book.stops), Decimal(0)),
        )

    @staticmethod
    def _order(o: Any) -> Order:
        price = o.limit_price or o.trigger_price
        return Order(
            order_link_id=o.order_link_id or o.exchange_order_id,
            strategy=o.strategy.value,
            symbol=o.symbol.value,
            purpose=PURPOSES.get(o.purpose.value, o.purpose.value),
            side=_side(o.qty.value),
            quantity=abs(o.qty.value),
            price=price.value if price else None,
            status=o.state.value,
            filled=o.exchange_cum_exec_qty,
            created_at=o.created_at,
        )

    @staticmethod
    def _fill(f: FillRecord) -> Fill:
        e = f.event
        return Fill(
            exec_id=e.exec_id,
            order_link_id=e.order_link_id or e.exchange_order_id,
            symbol=e.symbol.value,
            side=_side(e.qty.value),
            price=e.price.value,
            quantity=abs(e.qty.value),
            fee=e.fee,
            at=e.exec_time,
        )

    @staticmethod
    def _decision(d: DecisionRecord) -> Decision:
        rejected = ", ".join(r.code.value for r in d.rejections)
        return Decision(
            signal_id=d.signal_id,
            at=d.decided_at,
            strategy=d.strategy.value,
            symbol=d.symbol.value,
            ai_score=None,
            reason=f"{d.kind.value}" + (f": {rejected}" if rejected else ""),
            cost_usd=None,
        )
