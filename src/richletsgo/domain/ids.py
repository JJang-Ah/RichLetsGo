"""Order identity codecs with explicitly injected unique-value generators."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from .enums import Environment, OrderPurpose, StrategyId

_ENV_CODES = {Environment.BACKTEST: "b", Environment.DEMO: "d", Environment.LIVE: "l"}
_ENV_VALUES = {code: env for env, code in _ENV_CODES.items()}
_ORDER_ID = re.compile(r"([bdl])-(S[12])-([EXP])-([A-Za-z0-9_-]{1,29})", re.ASCII)


@dataclass(frozen=True, slots=True)
class OrderLinkIdParts:
    environment: Environment
    strategy: StrategyId
    purpose: OrderPurpose
    unique_value: str


def make_order_link_id(
    env: Environment,
    strategy: StrategyId,
    purpose: OrderPurpose,
    *,
    unique_factory: Callable[[], str],
) -> str:
    """Generate <=36 ASCII characters; factory must ensure uniqueness across restarts.

    The suffix is not truncated: truncation would silently weaken uniqueness.
    ATTACHED_SL identities are assigned by the exchange, never generated here.
    """
    if not isinstance(env, Environment) or not isinstance(strategy, StrategyId):
        raise TypeError("env and strategy must be domain enums")
    if not isinstance(purpose, OrderPurpose):
        raise TypeError("purpose must be OrderPurpose")
    if purpose is OrderPurpose.ATTACHED_SL:
        raise ValueError("attached stop IDs belong to the exchange")
    unique = unique_factory()
    if not isinstance(unique, str):
        raise TypeError("unique_factory must return str")
    result = f"{_ENV_CODES[env]}-{strategy.value}-{purpose.value}-{unique}"
    if parse_order_link_id(result) is None:
        raise ValueError("unique suffix must contain 1..29 ASCII letters, digits, '_' or '-'")
    return result


def parse_order_link_id(value: str) -> OrderLinkIdParts | None:
    """Return None for foreign/malformed IDs without whitespace or case normalization."""
    if not isinstance(value, str):
        raise TypeError("order link ID must be str")
    match = _ORDER_ID.fullmatch(value)
    if match is None:
        return None
    env, strategy, purpose, unique = match.groups()
    return OrderLinkIdParts(_ENV_VALUES[env], StrategyId(strategy), OrderPurpose(purpose), unique)


def new_operation_id(*, uuid_factory: Callable[[], UUID]) -> str:
    """Return a canonical UUID string; caller supplies production/test entropy."""
    value = uuid_factory()
    if not isinstance(value, UUID):
        raise TypeError("uuid_factory must return UUID")
    return str(value)
