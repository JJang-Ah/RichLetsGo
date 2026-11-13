"""Versioned, allow-listed domain JSON; never import types named by database content."""

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from richletsgo.backtest.results import Trade
from richletsgo.domain import enums, events, values
from richletsgo.marketdata.models import Bar, Interval
from richletsgo.oms import models as orders
from richletsgo.portfolio import models as portfolio
from richletsgo.portfolio.signals import MarketSignal
from richletsgo.risk import equity, records, resume
from richletsgo.strategy.models import S1State, Trend

from . import contracts

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
TYPES: dict[str, Any] = {
    cls.__name__: cls
    for module in (enums, events, values, orders, portfolio, records, contracts, equity, resume)
    for cls in vars(module).values()
    if isinstance(cls, type) and (is_dataclass(cls) or issubclass(cls, Enum))
}


TYPES.update({c.__name__: c for c in (Bar, Interval, MarketSignal, S1State, Trend, Trade)})


def milliseconds(value: datetime) -> int:
    if value.utcoffset() != timedelta(0):
        raise ValueError("UTC datetime required")
    return (value - EPOCH) // timedelta(milliseconds=1)


def decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("finite Decimal required")
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def pack(value: Any) -> Any:
    if isinstance(value, Enum):
        return {"enum": type(value).__name__, "value": value.value}
    if isinstance(value, Decimal):
        return {"decimal": decimal_text(value)}
    if isinstance(value, datetime):
        return {"utc_ms": milliseconds(value)}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "type": type(value).__name__,
            "fields": {f.name: pack(getattr(value, f.name)) for f in fields(value)},
        }
    if isinstance(value, list):
        return {"list": [pack(v) for v in value]}
    if isinstance(value, tuple):
        return {"tuple": [pack(v) for v in value]}
    if isinstance(value, frozenset):
        return {"frozenset": [pack(v) for v in sorted(value)]}
    if isinstance(value, Mapping):
        return {"map": [[pack(k), pack(v)] for k, v in value.items()]}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported persistent value: {type(value).__name__}")


def unpack(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "decimal" in value:
        return Decimal(value["decimal"])
    if "utc_ms" in value:
        return EPOCH + timedelta(milliseconds=value["utc_ms"])
    if "enum" in value:
        return TYPES[value["enum"]](value["value"])
    if "type" in value:
        return TYPES[value["type"]](**{k: unpack(v) for k, v in value["fields"].items()})
    if "list" in value:
        return [unpack(v) for v in value["list"]]
    if "tuple" in value:
        return tuple(unpack(v) for v in value["tuple"])
    if "frozenset" in value:
        return frozenset(unpack(v) for v in value["frozenset"])
    if "map" in value:
        return {unpack(k): unpack(v) for k, v in value["map"]}
    raise ValueError("unknown persisted encoding")


def encode(value: Any) -> str:
    return json.dumps(pack(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode(value: str) -> Any:
    return unpack(json.loads(value))
