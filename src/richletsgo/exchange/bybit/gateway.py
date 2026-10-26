"""Demo ExchangeGateway. Account/sync checks only gate entries; protection stays available."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from decimal import InvalidOperation
from typing import Any

from richletsgo.domain import (
    ExecutionEvent,
    InstrumentSpec,
    OrderEvent,
    OrderPurpose,
    PositionSnapshot,
    Price,
    Qty,
    Symbol,
)
from richletsgo.domain.clock import require_utc
from richletsgo.exchange.contracts import (
    Accepted,
    CommandResult,
    ErrorCode,
    ExchangeError,
    OrderRequest,
    PrivateEvent,
)
from richletsgo.marketdata.models import RiskLimitTier, from_ms, to_ms
from richletsgo.risk.equity import AccountSnapshot, CashFlow
from richletsgo.risk.pause import Pause, PauseKind, PauseState

from . import mapping
from .rest import RestClient

MAPPING_ERRORS = (KeyError, ValueError, TypeError, IndexError, InvalidOperation, OverflowError)


class BybitGateway:
    def __init__(self, rest: RestClient, pauses: PauseState, alert: Callable[[str], None]) -> None:
        self.rest, self.config, self.clock = rest, rest.config, rest.clock
        self.pauses, self.alert = pauses, alert
        self._connected = False
        self._settings_ok = False
        self._products_ok = False
        self._clock_checked: datetime | None = None
        self._clock_ok = False
        self._synchronized = False
        self.clock_offset_ms: int | None = None
        self._events: asyncio.Queue[PrivateEvent] = asyncio.Queue()
        self.wallet_events: asyncio.Queue[AccountSnapshot] = asyncio.Queue()
        self._orders: dict[str, OrderEvent] = {}
        self._requests: dict[str, OrderRequest] = {}
        self._instruments: dict[Symbol, InstrumentSpec] = {}
        self.tier_one: dict[Symbol, RiskLimitTier] = {}
        self._refreshed: datetime | None = None
        self.pause("bybit_startup")

    @property
    def connected(self) -> bool:
        return self._connected

    def pause(self, reason: str) -> None:
        if self.pauses.add(Pause(PauseKind.ENTRY_PAUSED_SYSTEM, reason, self.clock.now())):
            self.alert(reason)

    def _bad(self, reason: str) -> ExchangeError:
        self.pause(reason)
        return ExchangeError(ErrorCode.QUERY_FAILED, reason)

    def stream_state(self, connected: bool) -> None:
        self._connected = connected
        self._synchronized = False
        self.pause("bybit_stream")

    def synchronized(self, success: bool) -> None:
        """Called only with the result of M1-06 synchronize(), after WS subscription."""
        self._synchronized = success and self.connected
        if self._synchronized:
            self.pauses.clear(PauseKind.ENTRY_PAUSED_SYSTEM, reason="bybit_stream")

    async def check_clock(self) -> bool:
        start = to_ms(self.clock.now())
        response = await self.rest.call("GET", "/v5/market/time", {}, public=True)
        self._clock_checked = self.clock.now()
        try:
            if isinstance(response, ExchangeError):
                raise ValueError
            server = int(response["result"]["timeNano"]) // 1_000_000
            end = to_ms(self.clock.now())
            self.clock_offset_ms = server - (start + end) // 2
            # Include half RTT as uncertainty; do not certify a very slow time request.
            self._clock_ok = (
                abs(self.clock_offset_ms) + (end - start) // 2 <= self.config.clock_limit_ms
            )
        except MAPPING_ERRORS:
            self._clock_ok = False
        logging.getLogger("richletsgo.Trader").info(
            "Bybit clock sample",
            extra={"fields": {"offset_ms": self.clock_offset_ms, "within_limit": self._clock_ok}},
        )
        if not self._clock_ok:
            self.pause("clock")
        else:
            self.pauses.clear(PauseKind.ENTRY_PAUSED_SYSTEM, reason="clock")
        return self._clock_ok

    async def check_settings(self) -> bool:
        info = await self.rest.call("GET", "/v5/account/info", {})
        valid = (
            not isinstance(info, ExchangeError)
            and info["result"].get("marginMode") == "ISOLATED_MARGIN"
        )
        for symbol in self.config.symbols:
            rows = await self.rest.pages(
                "/v5/position/list", {"category": "linear", "symbol": symbol.value}
            )
            try:
                if isinstance(rows, ExchangeError) or len(rows) != 1:
                    raise ValueError
                row = rows[0]
                valid = (
                    valid
                    and row["symbol"] == symbol.value
                    and row["positionIdx"] == 0
                    and (mapping.number(row["leverage"]) == self.config.leverage)
                )
            except MAPPING_ERRORS:
                valid = False
        self._settings_ok = valid
        if not valid:
            self.pause("bybit_account_settings")
        else:
            self.pauses.clear(PauseKind.ENTRY_PAUSED_SYSTEM, reason="bybit_account_settings")
        return valid

    async def initialize(self) -> bool:
        clock_ok = await self.check_clock()
        settings_ok = await self.check_settings()
        products_ok = await self.refresh_instruments()
        good = clock_ok and settings_ok and products_ok
        if good:
            self.pauses.clear(PauseKind.ENTRY_PAUSED_SYSTEM, reason="bybit_startup")
        return good

    async def maintain(self) -> None:
        """Run as a service task; cancellation stops it. No order/account settings writes."""
        while True:
            await self.rest.sleep(self.config.clock_check_seconds)
            await self.check_clock()
            if self._refreshed is None or self.clock.now() - self._refreshed >= timedelta(days=1):
                await self.refresh_instruments()

    async def place_order(self, request: OrderRequest) -> CommandResult:
        if request.symbol not in self.config.symbols or len(request.order_link_id) > 36:
            return ExchangeError(ErrorCode.REJECTED, "invalid order identity")
        if request.expires_at is not None and request.expires_at <= self.clock.now():
            return ExchangeError(ErrorCode.REJECTED, "order expired before submission")
        if request.purpose is OrderPurpose.ENTRY:
            if self._clock_checked is None or self.clock.now() - self._clock_checked >= timedelta(
                seconds=self.config.clock_check_seconds
            ):
                await self.check_clock()
            if not (
                self.connected
                and self._synchronized
                and self._clock_ok
                and self._settings_ok
                and self._products_ok
                and self._refreshed is not None
                and self.clock.now() - self._refreshed < timedelta(days=1)
            ):
                return ExchangeError(ErrorCode.REJECTED, "Bybit entry readiness failed")
            if (
                request.stop is None
                or request.trigger_price is not None
                or request.stop.trigger_by != self.config.trigger_by
            ):
                return ExchangeError(ErrorCode.REJECTED, "entry attached SL required")
            if (
                request.stop.stop_loss.value
                - (request.limit_price or request.reference_price).value
            ) * request.qty.value >= 0:
                return ExchangeError(ErrorCode.REJECTED, "entry SL direction invalid")
        elif request.purpose in (OrderPurpose.PROTECT, OrderPurpose.ATTACHED_SL):
            if request.trigger_price is None:
                return ExchangeError(ErrorCode.REJECTED, "protective trigger required")
        if request.reduce_only and request.stop is not None:
            return ExchangeError(ErrorCode.REJECTED, "only entries attach stops")
        if request.purpose is OrderPurpose.CLOSE and request.trigger_price is not None:
            return ExchangeError(ErrorCode.REJECTED, "close must not be conditional")
        body: dict[str, Any] = {
            "category": "linear",
            "symbol": request.symbol.value,
            "orderLinkId": request.order_link_id,
            "side": "Buy" if request.qty.value > 0 else "Sell",
            "qty": str(abs(request.qty.value)),
            "positionIdx": 0,
            "orderType": "Limit" if request.limit_price else "Market",
            "timeInForce": "GTC" if request.limit_price else "IOC",
        }
        if request.limit_price:
            body["price"] = str(request.limit_price.value)
        if request.reduce_only:
            body["reduceOnly"] = True
        else:
            assert request.stop is not None
            body.update(request.stop.payload())
        if request.trigger_price:
            body.update(
                triggerPrice=str(request.trigger_price.value),
                triggerDirection=1 if request.qty.value > 0 else 2,
                triggerBy=self.config.trigger_by,
            )
        self._requests[request.order_link_id] = request  # retains UNKNOWN lookup symbol
        return await self._command("/v5/order/create", body)

    async def _command(self, path: str, body: dict[str, Any]) -> CommandResult:
        reply = await self.rest.call("POST", path, body)
        if isinstance(reply, ExchangeError):
            return reply
        result = reply["result"]
        order_id = result.get("orderId")
        if not isinstance(order_id, str) or not order_id:
            return ExchangeError(ErrorCode.TIMEOUT, "Bybit command acknowledgement incomplete")
        if (body.get("orderId") and body["orderId"] != order_id) or (
            body.get("orderLinkId")
            and result.get("orderLinkId")
            and body["orderLinkId"] != result["orderLinkId"]
        ):
            return ExchangeError(ErrorCode.TIMEOUT, "Bybit acknowledgement identity mismatch")
        return Accepted(result.get("orderLinkId") or body.get("orderLinkId"), order_id)

    def _remember(self, event: OrderEvent) -> None:
        # Cache only identity/immutable side; never suppress duplicates/out-of-order events.
        self._orders[event.exchange_order_id] = event
        if event.order_link_id:
            self._orders[event.order_link_id] = event

    async def _identity(self, key: str) -> dict[str, Any] | ExchangeError:
        event = self._orders.get(key)
        if event:
            return {
                "category": "linear",
                "symbol": event.symbol.value,
                "orderId": event.exchange_order_id,
            }
        request = self._requests.get(key)
        if request:
            return {"category": "linear", "symbol": request.symbol.value, "orderLinkId": key}
        # On restart an exchange-generated SL id has no client-id syntax. Discover it
        # by complete open snapshots, never guess an identity namespace or symbol.
        # Terminal history is populated by synchronization; absence here is NOT_FOUND,
        # never proof of cancellation/fill (OMS owns that decision).
        for symbol in self.config.symbols:
            events = await self.get_open_orders(symbol)
            if isinstance(events, ExchangeError):
                return events
            for event in events:
                if key in (event.order_link_id, event.exchange_order_id):
                    return {
                        "category": "linear",
                        "symbol": symbol.value,
                        "orderId": event.exchange_order_id,
                    }
        return ExchangeError(ErrorCode.NOT_FOUND, "order identity not found")

    async def cancel_order(self, order_link_id: str) -> CommandResult:
        body = await self._identity(order_link_id)
        if isinstance(body, ExchangeError):
            return body
        return await self._command("/v5/order/cancel", body)

    async def amend_stop(
        self, order_link_id: str, trigger_price: Price | None = None, qty: Qty | None = None
    ) -> CommandResult:
        if trigger_price is None and qty is None:
            return ExchangeError(ErrorCode.REJECTED, "empty amendment")
        body = await self._identity(order_link_id)
        if isinstance(body, ExchangeError):
            return body
        event = self._orders.get(order_link_id)
        request = self._requests.get(order_link_id)
        old_qty = event.order_qty if event else request.qty if request else None
        protective = (event is not None and event.reduce_only and event.stop_kind is not None) or (
            request is not None and request.reduce_only and request.trigger_price is not None
        )
        if not protective or old_qty is None:
            return ExchangeError(ErrorCode.REJECTED, "confirmed protective identity required")
        if qty is not None:
            if qty.value * old_qty.value <= 0:
                return ExchangeError(ErrorCode.REJECTED, "amendment direction invalid")
            body["qty"] = str(abs(qty.value))  # TOTAL quantity, never remaining quantity
        if trigger_price is not None:
            body["triggerPrice"] = str(trigger_price.value)
        return await self._command("/v5/order/amend", body)

    async def get_open_orders(self, symbol: Symbol) -> tuple[OrderEvent, ...] | ExchangeError:
        events: list[OrderEvent] = []
        for kind in ("Order", "StopOrder"):
            rows = await self.rest.pages(
                "/v5/order/realtime",
                {
                    "category": "linear",
                    "symbol": symbol.value,
                    "openOnly": 0,
                    "orderFilter": kind,
                    "limit": self.config.page_limit,
                },
            )
            if isinstance(rows, ExchangeError):
                return rows
            try:
                for row in rows:
                    event = mapping.order(row)
                    if event.symbol is not symbol:
                        raise ValueError
                    self._remember(event)
                    events.append(event)
            except MAPPING_ERRORS:
                return self._bad("bybit_order_mapping")
        return tuple(events)

    def _range(self, since: datetime) -> dict[str, int] | ExchangeError:
        require_utc(since)
        now = self.clock.now()
        if not now - timedelta(days=7) <= since <= now:
            return self._bad("bybit_history_window")
        return {"startTime": to_ms(since), "endTime": to_ms(now), "limit": self.config.page_limit}

    async def get_order_history(
        self, symbol: Symbol, since: datetime
    ) -> tuple[OrderEvent, ...] | ExchangeError:
        bounds = self._range(since)
        if isinstance(bounds, ExchangeError):
            return bounds
        rows = await self.rest.pages(
            "/v5/order/history", {"category": "linear", "symbol": symbol.value, **bounds}
        )
        if isinstance(rows, ExchangeError):
            return rows
        try:
            events = tuple(mapping.order(r) for r in rows)
            for event in events:
                if event.symbol is not symbol:
                    raise ValueError
                self._remember(event)
            return events
        except MAPPING_ERRORS:
            return self._bad("bybit_order_mapping")

    async def get_executions(
        self, symbol: Symbol, since: datetime
    ) -> tuple[ExecutionEvent, ...] | ExchangeError:
        bounds = self._range(since)
        if isinstance(bounds, ExchangeError):
            return bounds
        rows = await self.rest.pages(
            "/v5/execution/list", {"category": "linear", "symbol": symbol.value, **bounds}
        )
        if isinstance(rows, ExchangeError):
            return rows
        try:
            events = tuple(mapping.execution(r) for r in rows if r["execType"] != "Funding")
            if any(e.symbol is not symbol for e in events):
                raise ValueError
            return events
        except MAPPING_ERRORS:
            return self._bad("bybit_execution_mapping")

    async def get_positions(self) -> tuple[PositionSnapshot, ...] | ExchangeError:
        result = []
        for symbol in self.config.symbols:
            rows = await self.rest.pages(
                "/v5/position/list", {"category": "linear", "symbol": symbol.value}
            )
            if isinstance(rows, ExchangeError):
                return rows
            try:
                if len(rows) != 1 or rows[0]["symbol"] != symbol.value:
                    raise ValueError
                result.append(mapping.position(rows[0], self.clock.now()))
            except MAPPING_ERRORS:
                return self._bad("bybit_position_mapping")
        return tuple(result)

    async def get_account(self) -> AccountSnapshot | ExchangeError:
        reply = await self.rest.call(
            "GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"}
        )
        if isinstance(reply, ExchangeError):
            return reply
        try:
            rows = reply["result"]["list"]
            if len(rows) != 1:
                raise ValueError
            return mapping.account(rows[0], from_ms(int(reply["time"])))
        except MAPPING_ERRORS:
            return self._bad("bybit_wallet_mapping")

    async def get_cash_flows(self, since: datetime) -> tuple[CashFlow, ...] | ExchangeError:
        bounds = self._range(since)
        if isinstance(bounds, ExchangeError):
            return bounds
        # Do not filter category=linear: wallet transfers can have an empty category.
        rows = await self.rest.pages(
            "/v5/account/transaction-log", {"accountType": "UNIFIED", "currency": "USDT", **bounds}
        )
        if isinstance(rows, ExchangeError):
            return rows
        try:
            return tuple(flow for row in rows if (flow := mapping.cash_flow(row)) is not None)
        except MAPPING_ERRORS:
            return self._bad("bybit_cashflow_mapping")

    async def refresh_instruments(self) -> bool:
        self._products_ok = False
        specs: dict[Symbol, InstrumentSpec] = {}
        tiers: dict[Symbol, RiskLimitTier] = {}
        at = self.clock.now()
        for symbol in self.config.symbols:
            params = {"category": "linear", "symbol": symbol.value}
            products = await self.rest.pages("/v5/market/instruments-info", params, public=True)
            risks = await self.rest.pages("/v5/market/risk-limit", params, public=True)
            try:
                if isinstance(products, ExchangeError) or isinstance(risks, ExchangeError):
                    raise ValueError
                if len(products) != 1 or not risks or products[0]["symbol"] != symbol.value:
                    raise ValueError
                p = products[0]
                specs[symbol] = InstrumentSpec(
                    symbol,
                    mapping.number(p["lotSizeFilter"]["qtyStep"]),
                    mapping.number(p["lotSizeFilter"]["minOrderQty"]),
                    mapping.number(p["priceFilter"]["tickSize"]),
                    mapping.number(p["leverageFilter"]["maxLeverage"]),
                )
                r = min(risks, key=lambda row: mapping.number(row["riskLimitValue"]))
                if any(row["symbol"] != symbol.value for row in risks):
                    raise ValueError
                tiers[symbol] = RiskLimitTier(
                    symbol,
                    1,
                    mapping.number(r["riskLimitValue"]),
                    mapping.number(r["maintenanceMargin"]),
                    mapping.number(r["initialMargin"]),
                    mapping.number(r["maxLeverage"]),
                    at,
                )
            except MAPPING_ERRORS:
                self.pause("bybit_instruments")
                return False
        self._instruments, self.tier_one, self._refreshed = specs, tiers, at
        self._products_ok = True
        self.pauses.clear(PauseKind.ENTRY_PAUSED_SYSTEM, reason="bybit_instruments")
        return True

    async def get_instrument(self, symbol: Symbol) -> InstrumentSpec | ExchangeError:
        if self._refreshed is None or self.clock.now() - self._refreshed >= timedelta(days=1):
            if not await self.refresh_instruments():
                return self._bad("bybit_instruments")
        return self._instruments.get(symbol) or self._bad("bybit_unknown_instrument")

    async def on_private_message(self, message: dict[str, Any]) -> None:
        try:
            topic = message["topic"]
            at = from_ms(int(message["creationTime"]))
            for row in message["data"]:
                if topic == "wallet":
                    # Wallet pushes do not cover every mark-price PnL change; REST remains required.
                    self.wallet_events.put_nowait(mapping.account(row, at))
                    continue
                if row.get("category", "linear") != "linear":
                    raise ValueError
                event: PrivateEvent
                if topic == "order":
                    event = mapping.order(row)
                    self._remember(event)
                elif topic == "execution":
                    if row["execType"] == "Funding":
                        continue
                    event = mapping.execution(row)
                elif topic == "position":
                    event = mapping.position(row, at)
                else:
                    raise ValueError
                self._events.put_nowait(event)
        except MAPPING_ERRORS:
            self.pause("bybit_private_mapping")
            raise ValueError("Bybit private event invalid") from None

    async def private_events(self) -> AsyncIterator[PrivateEvent]:
        while True:
            yield await self._events.get()
