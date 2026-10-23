"""Single send boundary: commit-before-entry, journal-before-protection (D45/D60)."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from richletsgo.domain import Clock, OrderPurpose, OrderState, Price, Qty, StrategyId, Symbol
from richletsgo.exchange.contracts import (
    Accepted,
    CommandResult,
    ErrorCode,
    ExchangeError,
    ExchangeGateway,
    OrderRequest,
)
from richletsgo.portfolio.models import StopParameters
from richletsgo.portfolio.trailing import ingest_trail_record
from richletsgo.risk.pause import Pause, PauseKind, PauseState
from richletsgo.storage.contracts import UnitOfWork

from .journal import JournalCorrupt, JournalRecord, ProtectiveJournal
from .models import OrderRecord
from .recovery_index import store_record


def encode_order(order: OrderRecord) -> dict[str, Any]:
    return {
        "link": order.order_link_id,
        "exchange": order.exchange_order_id,
        "parent": order.parent_order_link_id,
        "strategy": order.strategy.value,
        "purpose": order.purpose.value,
        "symbol": order.symbol.value,
        "qty": str(order.qty.value),
        "limit": str(order.limit_price.value) if order.limit_price else None,
        "reference": str(order.reference_price.value),
        "created": order.created_at.isoformat(),
        "trigger": str(order.trigger_price.value) if order.trigger_price else None,
        "trigger_by": order.trigger_by,
        "stop": str(order.attached_stop.stop_loss.value) if order.attached_stop else None,
        "stop_by": order.attached_stop.trigger_by if order.attached_stop else None,
    }


def decode_order(data: dict[str, Any]) -> OrderRecord:
    at = datetime.fromisoformat(data["created"])
    return OrderRecord(
        data["link"],
        data["exchange"],
        data["parent"],
        StrategyId(data["strategy"]),
        OrderPurpose(data["purpose"]),
        Symbol(data["symbol"]),
        Qty(Decimal(data["qty"])),
        Price(Decimal(data["limit"])) if data["limit"] else None,
        Price(Decimal(data["reference"])),
        OrderState.UNKNOWN,
        Qty(Decimal(0)),
        Decimal(0),
        at,
        at,
        StopParameters(Price(Decimal(data["stop"])), data["stop_by"]) if data["stop"] else None,
        Price(Decimal(data["trigger"])) if data["trigger"] else None,
        data["trigger_by"],
    )


class OrderSender:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        gateway: ExchangeGateway,
        journal: ProtectiveJournal,
        clock: Clock,
        pauses: PauseState,
        unique: Callable[[], str],
        alerts: list[str],
    ) -> None:
        self.uow_factory, self.gateway, self.journal = uow_factory, gateway, journal
        self.clock, self.pauses, self.unique, self.alerts = clock, pauses, unique, alerts
        self._claimed: set[str] = set()

    def pause(self, reason: str, symbol: Symbol | None = None) -> None:
        self.pauses.add(
            Pause(PauseKind.ENTRY_PAUSED_SYSTEM, reason, self.clock.now(), symbol=symbol)
        )
        if reason not in self.alerts:
            self.alerts.append(reason)

    async def _save(self, record: JournalRecord) -> None:
        async with self.uow_factory() as uow:
            store_record(uow, record)
            ingest_trail_record(uow, record)
            if record.action_key:
                uow.portfolio.acknowledge(record.action_key)
            await uow.commit()

    async def send(
        self,
        order: OrderRecord,
        *,
        action_key: str | None = None,
        group_id: str | None = None,
        step: int | None = None,
        attempt: int | None = None,
    ) -> CommandResult:
        entry = order.purpose is OrderPurpose.ENTRY
        if entry and self.pauses.blocking(order.strategy, order.symbol):
            return ExchangeError(ErrorCode.REJECTED, "entries paused")
        if action_key and action_key in self._claimed:
            return ExchangeError(ErrorCode.TIMEOUT, "operation already owned; reconcile")
        try:
            async with self.uow_factory() as uow:
                current = uow.orders.get(order.key)
                if current is None or current.state is not OrderState.CREATED:
                    return ExchangeError(
                        ErrorCode.REJECTED, "only persisted CREATED orders may be sent"
                    )
                order = current
                order.request_payload()  # validate attached protection before commit
                uow.orders.put(
                    replace(
                        order,
                        state=OrderState.SUBMITTED,
                        updated_at=max(order.updated_at, self.clock.now()),
                    )
                )
                if entry and action_key:
                    raise ValueError("entries do not use portfolio outbox")
                await uow.commit()
        except OSError:
            self.pause("db")
            if entry:
                return ExchangeError(ErrorCode.REJECTED, "entry commit failed; not sent")
        if entry:
            try:
                result = await self.gateway.place_order(OrderRequest.from_record(order))
            except (OSError, TimeoutError):
                result = ExchangeError(ErrorCode.TIMEOUT, "entry transport outcome unknown")
        else:
            payload = {
                "reduce_only": True,
                "order": encode_order(order),
                "symbol": order.symbol.value,
            }
            result = await self.protective(
                "close" if order.purpose is OrderPurpose.CLOSE else "create",
                order.order_link_id or order.exchange_order_id or "",
                payload,
                lambda: self.gateway.place_order(OrderRequest.from_record(order)),
                action_key=action_key,
                group_id=group_id,
                step=step,
                attempt=attempt,
            )
        await self.record_result(order, result)
        return result

    async def record_result(self, order: OrderRecord, result: CommandResult) -> None:
        try:
            async with self.uow_factory() as uow:
                current = uow.orders.get(order.key)
                if current is not None and current.state in (
                    OrderState.CREATED,
                    OrderState.SUBMITTED,
                ):
                    state = (
                        OrderState.SUBMITTED
                        if isinstance(result, Accepted)
                        else OrderState.UNKNOWN
                        if result.code is ErrorCode.TIMEOUT
                        else OrderState.REJECTED
                    )
                    uow.orders.put(
                        replace(
                            current,
                            state=state,
                            exchange_order_id=result.exchange_order_id
                            if isinstance(result, Accepted)
                            else current.exchange_order_id,
                            updated_at=max(current.updated_at, self.clock.now()),
                        )
                    )
                await uow.commit()
        except OSError:
            self.pause("db")

    async def protective(
        self,
        op_type: str,
        target: str,
        payload: dict[str, Any],
        call: Callable[[], Awaitable[CommandResult]],
        *,
        action_key: str | None = None,
        group_id: str | None = None,
        step: int | None = None,
        attempt: int | None = None,
    ) -> CommandResult:
        if action_key and action_key in self._claimed:
            return ExchangeError(ErrorCode.TIMEOUT, "operation already owned; reconcile")
        record = JournalRecord(
            self.unique(),
            "requested",
            0,
            op_type,
            target,
            json.dumps(payload, sort_keys=True),
            "{}",
            self.clock.now().isoformat(),
            action_key,
            group_id,
            step,
            attempt,
        )
        try:
            record = self.journal.append(record)
        except (OSError, JournalCorrupt):
            self.pause("journal_uncertain")
        if action_key:
            self._claimed.add(action_key)
        try:
            await self._save(record)
        except OSError:
            self.pause("db")
        try:
            result: CommandResult = await call()
        except (OSError, TimeoutError):
            result = ExchangeError(ErrorCode.TIMEOUT, "transport outcome unknown")
        response = {"accepted": isinstance(result, Accepted)}
        encoded = json.dumps(
            {
                **response,
                "code": result.code.value if isinstance(result, ExchangeError) else None,
                "exchange_id": result.exchange_order_id if isinstance(result, Accepted) else None,
            }
        )
        record = replace(record, phase="result", result=encoded, ts=self.clock.now().isoformat())
        try:
            record = self.journal.append(record)
        except (OSError, JournalCorrupt):
            self.pause("journal_uncertain")
        try:
            await self._save(record)
        except OSError:
            self.pause("db")
        return result

    async def cancel(
        self,
        order: OrderRecord,
        *,
        action_key: str | None = None,
        group_id: str | None = None,
        attempt: int | None = None,
    ) -> CommandResult:
        target = order.order_link_id or order.exchange_order_id
        assert target is not None
        return await self.protective(
            "cancel",
            target,
            {"symbol": order.symbol.value},
            lambda: self.gateway.cancel_order(target),
            action_key=action_key,
            group_id=group_id,
            step=3 if group_id else None,
            attempt=attempt,
        )

    async def amend(
        self,
        order: OrderRecord,
        *,
        trigger: Price | None = None,
        qty: Qty | None = None,
        action_key: str | None = None,
        trail_attempt: int | None = None,
    ) -> CommandResult:
        target = order.order_link_id or order.exchange_order_id
        assert target is not None
        fields = (["trigger"] if trigger else []) + (["qty"] if qty else [])
        if not fields:
            return ExchangeError(ErrorCode.REJECTED, "amend requires fields")
        payload: dict[str, Any] = {
            "symbol": order.symbol.value,
            "fields": fields,
            "before": {
                "trigger": str(order.trigger_price.value) if order.trigger_price else None,
                "qty": str(order.qty.value),
                "cumulative": str(order.exchange_cum_exec_qty),
            },
            "after": {
                "trigger": str(trigger.value) if trigger else None,
                "qty": str(qty.value) if qty else None,
            },
        }
        if trail_attempt is not None:
            payload["trail"] = {
                "order_key": order.key,
                "attempt": trail_attempt,
                "requested_at": self.clock.now().isoformat(),
            }
        return await self.protective(
            "amend",
            target,
            payload,
            lambda: self.gateway.amend_stop(target, trigger, qty),
            action_key=action_key,
        )
