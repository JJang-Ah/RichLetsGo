"""Strict V5 mappings. Missing/unknown fields fail reconciliation, never invent ownership."""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from richletsgo.domain import (
    ExecutionEvent,
    OrderEvent,
    OrderState,
    OrderType,
    PositionSnapshot,
    Price,
    Qty,
    StopKind,
    Symbol,
    TimeInForce,
)
from richletsgo.domain.values import finite_decimal
from richletsgo.marketdata.models import Bar, Interval, from_ms
from richletsgo.risk.equity import AccountSnapshot, CashFlow


def number(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("decimal string required")
    return finite_decimal(Decimal(value), "Bybit value")


def signed(value: object, side: str) -> Qty:
    amount = number(value)
    if amount < 0 or side not in ("Buy", "Sell"):
        raise ValueError("invalid Bybit quantity/side")
    return Qty(amount if side == "Buy" else -amount)


def flag(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("boolean required")
    return value


def order(row: Mapping[str, Any]) -> OrderEvent:
    states = {
        "New": OrderState.OPEN,
        "Untriggered": OrderState.OPEN,
        "Triggered": OrderState.OPEN,
        "PartiallyFilled": OrderState.PARTIALLY_FILLED,
        "Filled": OrderState.FILLED,
        "Cancelled": OrderState.CANCELLED,
        "PartiallyFilledCanceled": OrderState.CANCELLED,
        "Rejected": OrderState.REJECTED,
        "Deactivated": OrderState.CANCELLED,
    }
    stop_types = {
        "": None,
        "UNKNOWN": None,
        "PartialStopLoss": StopKind.PARTIAL_STOP_LOSS,
        "StopLoss": StopKind.STOP_LOSS,
        "Stop": StopKind.CONDITIONAL,
        "TakeProfit": StopKind.CONDITIONAL,
        "PartialTakeProfit": StopKind.CONDITIONAL,
        "TrailingStop": StopKind.CONDITIONAL,
    }
    kind = stop_types[row.get("stopOrderType", "")]
    trigger = row.get("triggerPrice")
    order_type = {"Market": OrderType.MARKET, "Limit": OrderType.LIMIT}[row["orderType"]]
    # A TP parent is not an SL ownership link. Preserve its original value in raw.
    parent = row.get("parentOrderLinkId") if kind is StopKind.PARTIAL_STOP_LOSS else None
    return OrderEvent(
        row.get("orderLinkId") or None,
        row["orderId"],
        parent or None,
        states[row["orderStatus"]],
        number(row["cumExecQty"]),
        from_ms(int(row["updatedTime"])),
        row,
        Symbol(row["symbol"]),
        signed(row["qty"], row["side"]),
        order_type,
        {
            "GTC": TimeInForce.GTC,
            "IOC": TimeInForce.IOC,
            "FOK": TimeInForce.FOK,
            "PostOnly": TimeInForce.POST_ONLY,
        }[row["timeInForce"]],
        Price(number(row["price"])) if order_type is OrderType.LIMIT else None,
        Price(number(trigger)) if trigger not in (None, "", "0") else None,
        flag(row["reduceOnly"]),
        kind,
    )


def execution(row: Mapping[str, Any]) -> ExecutionEvent:
    if row["execType"] not in ("Trade", "AdlTrade", "BustTrade"):
        raise ValueError("non-trade execution")
    return ExecutionEvent(
        row["execId"],
        row.get("orderLinkId") or None,
        row["orderId"],
        Symbol(row["symbol"]),
        signed(row["execQty"], row["side"]),
        Price(number(row["execPrice"])),
        number(row["execFee"]),
        from_ms(int(row["execTime"])),
        flag(row["isMaker"]),
    )


def position(row: Mapping[str, Any], at: datetime) -> PositionSnapshot:
    if row["positionIdx"] != 0:
        raise ValueError("hedge position is unsupported")
    qty = Qty(Decimal(0)) if number(row["size"]) == 0 else signed(row["size"], row["side"])
    # REST uses avgPrice, private position WS uses entryPrice.
    price = row.get("avgPrice", row.get("entryPrice"))
    return PositionSnapshot(
        Symbol(row["symbol"]), qty, Price(number(price)) if qty.value else None, at
    )


def account(row: Mapping[str, Any], at: datetime) -> AccountSnapshot:
    if row["accountType"] != "UNIFIED":
        raise ValueError("UNIFIED account required")
    coins = [r for r in row["coin"] if r["coin"] == "USDT"]
    if len(coins) != 1:
        raise ValueError("USDT wallet missing")
    coin = coins[0]
    if number(coin.get("spotBorrow", "0")) or number(coin.get("borrowAmount", "0")):
        raise ValueError("borrowed collateral unsupported")
    # Account-wide totals are USD and not applicable to UTA isolated margin.
    return AccountSnapshot(at, number(coin["walletBalance"]), number(coin["unrealisedPnl"]))


def cash_flow(row: Mapping[str, Any]) -> CashFlow | None:
    if row["currency"] != "USDT":
        raise ValueError("non-USDT transaction")
    kind = row["type"]
    if kind in ("TRADE", "SETTLEMENT", "LIQUIDATION", "ADL", "DELIVERY", "FEE_REFUND"):
        return None
    if kind not in ("TRANSFER_IN", "TRANSFER_OUT"):
        raise ValueError("unclassified account movement")
    amount = number(row["change"])
    if not amount:
        return None
    if (kind == "TRANSFER_IN") != (amount > 0):
        raise ValueError("transfer sign mismatch")
    return CashFlow(row["id"], from_ms(int(row["transactionTime"])), amount)


def bars(message: Mapping[str, Any]) -> tuple[Bar, ...]:
    prefix, code, name = message["topic"].split(".")
    if prefix != "kline" or code not in ("60", "D"):
        raise ValueError("unsupported kline topic")
    result = []
    for row in message["data"]:
        if not flag(row["confirm"]):
            continue
        if row["interval"] != code:
            raise ValueError("kline interval mismatch")
        result.append(
            Bar(
                Symbol(name),
                Interval(code),
                from_ms(int(row["start"])),
                *(number(row[k]) for k in ("open", "high", "low", "close", "volume", "turnover")),
            )
        )
    return tuple(result)
