"""Validated, immutable service configuration. No credentials and no implicit risk values."""

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

from richletsgo.backtest.codec import digest, primitive
from richletsgo.domain import Environment, StrategyId, Symbol
from richletsgo.risk.limits import RiskLimits
from richletsgo.strategy.models import S1AParams, S1BParams, S2AParams, S2BParams, S2PParams


class ConfigError(ValueError):
    """Only field names, never rejected input values, belong in configuration errors."""


def text(row: Mapping[str, Any], key: str) -> str:
    value = row[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"invalid text field: {key}")
    return value


def integer(row: Mapping[str, Any], key: str, minimum: int = 1) -> int:
    value = row[key]
    if type(value) is not int or value < minimum:
        raise ConfigError(f"invalid integer field: {key}")
    return value


def decimal(row: Mapping[str, Any], key: str) -> Decimal:
    value = Decimal(text(row, key))
    if not value.is_finite():
        raise ConfigError(f"invalid Decimal field: {key}")
    return value


@dataclass(frozen=True)
class Paths:
    journal_dir: Path
    log_dir: Path
    backup_dir: Path
    market_data_dir: Path | None = None
    backtest_results_dir: Path | None = None


@dataclass(frozen=True)
class BybitConfig:
    rest_url: str
    private_ws_url: str
    public_rest_url: str
    public_ws_url: str
    symbols: tuple[Symbol, ...]
    leverage: Decimal
    recv_window_ms: int
    timeout_seconds: int
    retry_attempts: int
    retry_delay_seconds: int
    clock_limit_ms: int
    clock_check_seconds: int
    ws_ping_seconds: int
    ws_backoff_seconds: int
    ws_max_backoff_seconds: int
    page_limit: int
    trigger_by: str


@dataclass(frozen=True)
class OracleConfig:
    dsn: str
    schema: str
    working_set_days: int


@dataclass(frozen=True)
class StrategyConfig:
    candidate: str
    params: S1AParams | S1BParams | S2AParams | S2BParams | S2PParams
    source: str


@dataclass(frozen=True)
class AppConfig:
    environment: Environment
    bybit: BybitConfig
    risk: RiskLimits
    ai_mode: str
    paths: Paths
    all_paths: Mapping[Environment, Paths]
    keystore: Mapping[str, str]
    api_created_at: datetime
    oracle: OracleConfig
    telegram_users: tuple[int, ...]
    strategies: Mapping[StrategyId, StrategyConfig]
    log_retention_days: int
    report_path: Path | None

    def public_dict(self) -> dict[str, Any]:
        return dict(primitive(self))

    @property
    def config_hash(self) -> str:
        return digest(self)


def risk_limits(row: Mapping[str, Any]) -> RiskLimits:
    shares = row["strategy_allocations"]
    if set(shares) != {"S1", "S2"}:
        raise ConfigError("strategy_allocations must contain S1 and S2")
    return RiskLimits(
        decimal(row, "risk_per_trade"),
        decimal(row, "daily_loss_limit_usdt"),
        decimal(row, "max_drawdown_ratio"),
        {s: decimal(shares, s.value) for s in StrategyId},
        integer(row, "consecutive_loss_limit"),
        timedelta(hours=integer(row, "cooldown_hours")),
        decimal(row, "max_leverage"),
        decimal(row, "max_symbol_notional_usdt"),
        decimal(row, "max_account_notional_usdt"),
        decimal(row, "liquidation_buffer_ratio"),
        decimal(row, "slippage_buffer"),
        decimal(row, "resume_equity_tolerance_ratio"),
    )


def strategy(row: Mapping[str, Any], owner: StrategyId) -> StrategyConfig:
    candidate, source = text(row, "candidate"), text(row, "source")
    classes = {
        "S1-A": S1AParams,
        "S1-B": S1BParams,
        "S2-A": S2AParams,
        "S2-B": S2BParams,
        "S2-P": S2PParams,
    }
    if candidate not in classes or not candidate.startswith(owner.value + "-"):
        raise ConfigError("unknown strategy candidate")
    if source != "manual" and re.fullmatch(r"v1:[0-9a-f]{64}", source) is None:
        raise ConfigError("invalid strategy source")
    cls = classes[candidate]
    raw = row["params"]
    if set(raw) != {f.name for f in fields(cls)}:
        raise ConfigError("strategy parameters must exactly match candidate")
    values = {
        f.name: decimal(raw, f.name) if f.type is Decimal else integer(raw, f.name)
        for f in fields(cls)
    }
    return StrategyConfig(candidate, cls(**values), source)


def validate_report(
    raw: Mapping[str, Any], strategies: Mapping[StrategyId, StrategyConfig], parent: Path
) -> Path | None:
    sources = {s.source for s in strategies.values()}
    if len(sources) != 1:
        raise ConfigError("S1 and S2 must use the same source")
    source = next(iter(sources))
    if source == "manual":
        return None  # Development only; M3-07 separately requires V1 PASS.
    target = Path(text(raw, "report_path")).expanduser()
    target = (target if target.is_absolute() else parent / target).resolve()
    body = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or digest(body) != source.removeprefix("v1:"):
        raise ConfigError("V1 body digest mismatch")
    identity = body["identity"]
    if not isinstance(identity, dict) or body["manifest_hash"] != digest(identity):
        raise ConfigError("V1 manifest link mismatch")
    registration = identity["preregistration"]
    if (
        not isinstance(registration, dict)
        or not isinstance(registration.get("id"), str)
        or not registration["id"].strip()
        or not isinstance(registration.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", registration["sha256"]) is None
    ):
        raise ConfigError("V1 preregistration link required")
    ordered = [strategies[s] for s in (StrategyId.S1, StrategyId.S2)]
    combo = "-".join(s.candidate.replace("-", "") for s in ordered)
    if body["aggregates"][combo]["verdict"] != "PASS":
        raise ConfigError("V1 combination must PASS")
    pair = primitive([s.params for s in ordered])
    if not any(
        w["status"] == "COMPLETED" and w["selected"] == pair for w in body["windows"][combo]
    ):
        raise ConfigError("V1 pair must match one completed confirmation window")
    return target


def _load(path: Path) -> AppConfig:
    path = path.resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    env = Environment(text(data, "environment"))
    if env is Environment.LIVE:
        raise ConfigError("live startup is disabled in M3")

    # Reject secret-bearing configuration rather than serializing it into config_versions.
    def inspect(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if re.sub(r"[^a-z]", "", key.lower()) in {
                    "password",
                    "secret",
                    "apisecret",
                    "apikey",
                    "token",
                    "bottoken",
                    "authorization",
                }:
                    raise ConfigError("credentials are forbidden in configuration")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(data)
    paths = {}
    for environment in Environment:
        raw = data["paths"][environment.value]
        required = {"journal_dir", "log_dir", "backup_dir"}
        if environment is Environment.BACKTEST:
            required |= {"market_data_dir", "backtest_results_dir"}
        if set(raw) != required:
            raise ConfigError("invalid environment path fields")
        resolved = {}
        for name in required:
            value = Path(text(raw, name)).expanduser()
            resolved[name] = (path.parent / value).resolve()
        paths[environment] = Paths(**resolved)
    for left in Environment:
        for right in Environment:
            if left.value >= right.value:
                continue
            for a in fields(Paths):
                for b in fields(Paths):
                    x, y = getattr(paths[left], a.name), getattr(paths[right], b.name)
                    if (
                        x is not None
                        and y is not None
                        and (x.is_relative_to(y) or y.is_relative_to(x))
                    ):
                        raise ConfigError("environment paths overlap")
    raw = data["bybit"]
    public = data["bybit_public"]
    endpoint = raw["demo"]  # No live endpoint can be selected by this loader.
    expected = {
        "rest_url": "https://api-demo.bybit.com",
        "private_ws_url": "wss://stream-demo.bybit.com/v5/private",
    }
    if any(text(endpoint, k) != v for k, v in expected.items()):
        raise ConfigError("demo endpoints required")
    if (
        text(public, "base_url") != "https://api.bybit.com"
        or text(public, "ws_url") != "wss://stream.bybit.com/v5/public/linear"
    ):
        raise ConfigError("mainnet public endpoints required")
    symbols = tuple(Symbol(s) for s in raw["symbols"])
    if (
        len(symbols) != 2
        or set(symbols) != {Symbol.BTCUSDT, Symbol.ETHUSDT}
        or text(raw, "category") != "linear"
    ):
        raise ConfigError("linear BTCUSDT and ETHUSDT required")
    leverage = decimal(raw, "leverage")
    if (
        not 3 <= leverage <= 5
        or integer(raw, "position_idx", 0) != 0
        or text(raw, "margin_mode") != "ISOLATED_MARGIN"
    ):
        raise ConfigError("one-way isolated configuration required")
    bybit = BybitConfig(
        expected["rest_url"],
        expected["private_ws_url"],
        public["base_url"],
        public["ws_url"],
        symbols,
        leverage,
        integer(raw, "recv_window_ms"),
        integer(raw, "timeout_seconds"),
        integer(raw, "retry_attempts"),
        integer(raw, "retry_delay_seconds"),
        integer(raw, "clock_limit_ms"),
        integer(raw, "clock_check_seconds"),
        integer(raw, "ws_ping_seconds"),
        integer(raw, "ws_backoff_seconds"),
        integer(raw, "ws_max_backoff_seconds"),
        integer(raw, "page_limit"),
        text(raw, "trigger_by"),
    )
    if (
        bybit.trigger_by != "LastPrice"
        or bybit.ws_ping_seconds > 20
        or bybit.ws_backoff_seconds < 1
        or bybit.ws_max_backoff_seconds < bybit.ws_backoff_seconds
        or bybit.page_limit > 50
    ):
        raise ConfigError("invalid Bybit transport policy")
    key_names = {
        "bybit_demo_api",
        "bybit_live_api",
        "telegram_bot",
        "oracle_demo",
        "oracle_live",
        "oracle_read",
    }
    keys = data["keystore"]
    if set(keys) != key_names:
        raise ConfigError("invalid keystore entry names")
    names = {k: text(keys, k) for k in key_names}
    if len(set(names.values())) != len(names):
        raise ConfigError("keystore entries must be distinct")
    created = datetime.fromisoformat(text(raw, "api_created_at").replace("Z", "+00:00"))
    if created.tzinfo is None or created.utcoffset() != timedelta(0):
        raise ConfigError("api_created_at must be UTC")
    oracle_raw = data["oracle"][env.value]
    oracle = OracleConfig(
        text(oracle_raw, "dsn"),
        text(oracle_raw, "schema"),
        integer(data["oracle"], "working_set_days", 7),
    )
    if oracle.schema != {Environment.DEMO: "RLG_DEMO", Environment.BACKTEST: "RLG_BACKTEST"}[env]:
        raise ConfigError("Oracle schema/environment mismatch")
    # DSN is a host/service descriptor, never user/password@host.
    if "@" in oracle.dsn or re.search(r"password\s*=", oracle.dsn, re.I):
        raise ConfigError("credentials are forbidden in Oracle DSN")
    users = data["telegram"]["allowed_user_ids"]
    if (
        not isinstance(users, list)
        or not users
        or any(type(v) is not int or v <= 0 for v in users)
        or len(set(users)) != len(users)
    ):
        raise ConfigError("telegram users must be unique positive IDs")
    mode = text(data["ai"], "mode")
    if mode != "disabled":
        raise ConfigError("M3 requires AI disabled")
    risk = risk_limits(
        data["backtest"]["research_risk"] if env is Environment.BACKTEST else data["risk"]
    )
    if bybit.leverage > risk.max_leverage:
        raise ConfigError("configured leverage exceeds risk limit")
    strategies = {s: strategy(data["strategy"][s.value], s) for s in StrategyId}
    report = validate_report(data["strategy"], strategies, path.parent)
    return AppConfig(
        env,
        bybit,
        risk,
        mode,
        paths[env],
        MappingProxyType(paths),
        MappingProxyType(names),
        created.astimezone(UTC),
        oracle,
        tuple(users),
        MappingProxyType(strategies),
        integer(data["logging"], "retention_days"),
        report,
    )


def load_config(path: Path) -> AppConfig:
    try:
        return _load(path)
    except ConfigError:
        raise
    except (KeyError, TypeError, ValueError, ArithmeticError, OSError):
        raise ConfigError(
            "invalid or missing configuration fields; check the documented schema"
        ) from None
