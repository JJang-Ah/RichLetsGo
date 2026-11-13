"""Trader assembly boundary; never starts a second trader or enables live trading."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

import uvicorn
from fastapi import FastAPI

from richletsgo.config import AppConfig
from richletsgo.domain import Clock, Environment
from richletsgo.keystore import KeyStore
from richletsgo.risk.gate import RiskGate
from richletsgo.storage.sqlite import SqliteDatabase

from .api import create_control_app
from .models import CommandPolicy
from .service import CommandService
from .telegram import TelegramCommands


@dataclass(frozen=True)
class ControlRuntime:
    service: CommandService
    api: FastAPI
    telegram: TelegramCommands


def build_control(
    config: AppConfig,
    database: SqliteDatabase,
    gate: RiskGate,
    clock: Clock,
    keys: KeyStore,
    notify: Callable[[str], Awaitable[None]],
) -> ControlRuntime:
    if config.environment is Environment.BACKTEST:
        raise ValueError("backtest cannot expose operator trading controls")
    if (
        database.environment is not config.environment
        or gate.account_id != database.account_id
        or gate.environment is not database.environment
    ):
        raise ValueError("control runtime account/environment mismatch")
    policy = config.control
    service = CommandService(
        account_id=database.account_id,
        environment=database.environment,
        uow_factory=database.unit_of_work,
        pauses=gate.pauses,
        tracker=gate.tracker,
        limits=gate.limits,
        allowed_ids=policy.allowed_ids,
        clock=clock,
        policy=CommandPolicy(
            timedelta(seconds=policy.cancel_retry_seconds), policy.cancel_max_attempts
        ),
        account_lock=gate.account_lock,
        notify=notify,
        resolve_commit=database.resolve,
    )
    api = create_control_app(
        service,
        operator_id=policy.operator_id,
        token=keys.get(config.keystore["control_token_" + config.environment.value]),
        port=policy.trader_port,
        dashboard_port=policy.dashboard_port,
    )
    return ControlRuntime(service, api, TelegramCommands(service))


async def serve_control(runtime: ControlRuntime, config: AppConfig) -> None:
    """Run alongside the existing Trader on its event loop, using the same account lock."""
    await runtime.service.startup()
    server = uvicorn.Server(
        uvicorn.Config(
            runtime.api,
            host="127.0.0.1",
            port=config.control.trader_port,
            access_log=False,
            proxy_headers=False,
        )
    )
    await server.serve()
