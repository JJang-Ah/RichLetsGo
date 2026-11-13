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
from richletsgo.storage.paths import local_database_path
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
class StorageConfig:
    db_file: Path
    working_set_days: int
    busy_timeout_seconds: int
    outcome_timeout_seconds: int


@dataclass(frozen=True)
class ControlConfig:
    dashboard_port: int
    trader_port: int
    operator_id: str
    allowed_ids: tuple[str, ...]
    cancel_retry_seconds: int
    cancel_max_attempts: int


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
    storage: StorageConfig
    telegram_users: tuple[int, ...]
    strategies: Mapping[StrategyId, StrategyConfig]
    log_retention_days: int
    report_path: Path | None
    control: ControlConfig
    secondary_report_path: Path | None = None

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
    def read(name: str, source: str) -> tuple[Path, dict[str, Any]]:
        target = (parent / Path(text(raw, name)).expanduser()).resolve()
        body = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(body, dict) or digest(body) != source.removeprefix("v1:"):
            raise ConfigError("V1 body digest mismatch")
        identity = body["identity"]
        if not isinstance(identity, dict):
            raise ConfigError("invalid V1 identity")
        registration = identity["preregistration"]
        if body["manifest_hash"] != digest(identity):
            raise ConfigError("V1 manifest link mismatch")
        if (
            not isinstance(registration, dict)
            or not isinstance(registration.get("id"), str)
            or not registration.get("id")
            or re.fullmatch(r"[0-9a-f]{64}", registration.get("sha256", "")) is None
        ):
            raise ConfigError("V1 preregistration link required")
        for key in ("data_hash", "start", "end"):
            if not identity.get(key):
                raise ConfigError("V1 data identity required")
        return target, body

    s1, s2 = strategies[StrategyId.S1], strategies.get(StrategyId.S2)
    if s2 is None and "secondary_report_path" in raw:
        raise ConfigError("disabled S2 cannot have secondary report")
    if s1.source == "manual":
        if s2 is not None and s2.source != "manual":
            raise ConfigError("manual and V1 cannot be mixed")
        if "secondary_report_path" in raw:
            raise ConfigError("manual cannot have secondary report")
        return None
    if s1.candidate != "S1-B":
        raise ConfigError("D97 requires primary S1-B")
    target, main = read("report_path", s1.source)
    if main["aggregates"]["S1B"]["verdict"] != "PASS":
        raise ConfigError("D97 primary must PASS")
    selected = primitive(s1.params)
    windows = [
        w
        for w in main["windows"]["S1B"]
        if w["status"] == "COMPLETED" and w["selected"] == [selected, None]
    ]
    if not windows:
        raise ConfigError("S1 must match primary selected value")
    if s2 is not None:
        if s2.candidate != "S2-P" or s2.source == "manual":
            raise ConfigError("D97 secondary requires S2-P V1")
        _, secondary = read("secondary_report_path", s2.source)
        if secondary["aggregates"]["S1B-S2P"]["verdict"] != "PASS":
            raise ConfigError("D97 secondary must PASS")
        for key in ("preregistration", "data_hash", "start", "end"):
            if main["identity"][key] != secondary["identity"][key]:
                raise ConfigError("D97 reports have different identities")
        default_symbols = ["BTCUSDT", "ETHUSDT"]
        if main["identity"].get("symbols", default_symbols) != secondary["identity"].get(
            "symbols", default_symbols
        ):
            raise ConfigError("D97 symbol mismatch")
        if not any(
            w["status"] == "COMPLETED"
            and w["selected"] == [selected, primitive(s2.params)]
            and any((w["start"], w["end"]) == (m["start"], m["end"]) for m in windows)
            for w in secondary["windows"]["S1B-S2P"]
        ):
            raise ConfigError("D97 pair must be selected in same confirmation window")
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
        "dashboard_access",
        "control_token_demo",
        "control_token_live",
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
    if "oracle" in data:
        raise ConfigError("replace obsolete oracle configuration with storage")
    storage_raw = data["storage"]
    db_files = {
        e: local_database_path(path.parent / text(storage_raw[e.value], "db_file"))
        for e in Environment
    }
    if len(set(db_files.values())) != len(Environment):
        raise ConfigError("environment database files must differ")
    storage = StorageConfig(
        db_files[env],
        integer(storage_raw, "working_set_days", 7),
        integer({"seconds": storage_raw.get("busy_timeout_seconds", 5)}, "seconds"),
        integer({"seconds": storage_raw.get("outcome_timeout_seconds", 10)}, "seconds"),
    )
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
    strategies = {StrategyId.S1: strategy(data["strategy"]["S1"], StrategyId.S1)}
    s2_row = data["strategy"]["S2"]
    enabled = s2_row.get("enabled", True)
    if type(enabled) is not bool:
        raise ConfigError("S2 enabled must be boolean")
    if enabled:
        strategies[StrategyId.S2] = strategy(s2_row, StrategyId.S2)
    elif set(s2_row) != {"enabled"}:
        raise ConfigError("disabled S2 only accepts enabled")
    report = validate_report(data["strategy"], strategies, path.parent)
    dashboard, control_raw = data["dashboard"], data["control"]
    control = ControlConfig(
        integer(dashboard, "port", 1024),
        integer(control_raw, "port", 1024),
        text(dashboard, "operator_id"),
        tuple(control_raw["allowed_ids"]),
        integer(control_raw, "cancel_retry_seconds"),
        integer(control_raw, "cancel_max_attempts"),
    )
    if (
        text(dashboard, "host") != "127.0.0.1"
        or text(control_raw, "host") != "127.0.0.1"
        or control.dashboard_port == control.trader_port
        or max(control.dashboard_port, control.trader_port) > 65535
        or not control.allowed_ids
        or any(
            not isinstance(v, str) or re.fullmatch(r"(?:web|telegram):[^\s:]+", v) is None
            for v in control.allowed_ids
        )
    ):
        raise ConfigError("invalid local control configuration")
    return AppConfig(
        env,
        bybit,
        risk,
        mode,
        paths[env],
        MappingProxyType(paths),
        MappingProxyType(names),
        created.astimezone(UTC),
        storage,
        tuple(users),
        MappingProxyType(strategies),
        integer(data["logging"], "retention_days"),
        report,
        control,
        (path.parent / text(data["strategy"], "secondary_report_path")).resolve()
        if "secondary_report_path" in data["strategy"]
        else None,
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
