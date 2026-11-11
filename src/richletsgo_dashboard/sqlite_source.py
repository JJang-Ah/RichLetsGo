"""Dashboard read projection using only SQLite mode=ro connections."""

import json
from decimal import Decimal
from pathlib import Path

from richletsgo.risk.equity import EquityTracker
from richletsgo.storage.codec import decode
from richletsgo.storage.sqlite import open_readonly

from .source import Decision, Fill, LogEntry, Order, Overview, Position, Snapshot


class SqliteSource:
    def __init__(self, path: Path) -> None:
        self.path = path

    def snapshot(self) -> Snapshot:
        connection = open_readonly(self.path)
        try:
            connection.execute("BEGIN")
            meta = dict(connection.execute("SELECT key,value FROM meta"))
            account = meta["account_id"]

            def rows(table: str, *, recent: bool = False) -> list[str]:
                # Identifiers are internal constants, never request parameters.
                query = f"SELECT payload FROM {table} WHERE account_id=?"
                if recent:
                    query += " ORDER BY updated_at DESC LIMIT 500"
                return [row[0] for row in connection.execute(query, (account,))]

            risk = dict(
                connection.execute(
                    "SELECT key,payload FROM risk_states WHERE account_id=?", (account,)
                )
            )
            tracker = (
                EquityTracker(decode(risk["equity_tracker"])) if "equity_tracker" in risk else None
            )
            pauses = json.loads(risk.get("pauses", "[]"))
            fills = [decode(v) for v in rows("fills", recent=True)]
            overview = Overview(
                meta["environment"],
                account,
                "HALTED"
                if any(p["kind"] == "HALTED" for p in pauses)
                else pauses[0]["kind"]
                if pauses
                else "ACTIVE"
                if tracker
                else "ENTRY_PAUSED_SYSTEM",
                tuple(p["reason"] for p in pauses) if tracker else ("위험 상태 스냅샷 미수신",),
                tracker.equity if tracker else Decimal(0),
                tracker.daily_pnl if tracker else Decimal(0),
                tracker.drawdown_ratio if tracker else Decimal(0),
                (),
                None,
                max((f.event.exec_time for f in fills), default=None),
                None,
                "disabled",
                False,
            )
            snapshot = Snapshot(overview)
            for raw in rows("books"):
                book = decode(raw)
                if book.qty:
                    snapshot.positions.append(
                        Position(
                            book.strategy.value,
                            book.symbol.value,
                            "LONG" if book.qty > 0 else "SHORT",
                            abs(book.qty),
                            book.avg_price,
                            Decimal(0),
                            book.stop_price.value if book.stop_price else None,
                            sum((s.qty for s in book.stops), Decimal(0)),
                        )
                    )
            for raw in rows("orders", recent=True):
                order = decode(raw)
                snapshot.orders.append(
                    Order(
                        order.order_link_id or order.exchange_order_id,
                        order.strategy.value,
                        order.symbol.value,
                        order.purpose.value,
                        "LONG" if order.qty.value > 0 else "SHORT",
                        abs(order.qty.value),
                        order.limit_price.value if order.limit_price else None,
                        order.state.value,
                        order.exchange_cum_exec_qty,
                        order.created_at,
                    )
                )
            for record in fills:
                fill = record.event
                snapshot.fills.append(
                    Fill(
                        fill.exec_id,
                        fill.order_link_id or fill.exchange_order_id,
                        fill.symbol.value,
                        "LONG" if fill.qty.value > 0 else "SHORT",
                        fill.price.value,
                        abs(fill.qty.value),
                        fill.fee,
                        fill.exec_time,
                    )
                )
            from datetime import datetime

            for raw in rows("commands"):
                record = json.loads(raw)
                snapshot.logs.append(
                    LogEntry(
                        datetime.fromisoformat(record["at"]),
                        "command",
                        record["source"],
                        record["command"] + ": " + record["message"],
                    )
                )
            for raw in rows("decisions", recent=True):
                decision = decode(raw)
                snapshot.decisions.append(
                    Decision(
                        decision.signal_id,
                        decision.decided_at,
                        decision.strategy.value,
                        decision.symbol.value,
                        decision.ai_score,
                        "; ".join(r.detail for r in decision.rejections),
                        Decimal(0),
                    )
                )
            return snapshot
        finally:
            connection.close()
