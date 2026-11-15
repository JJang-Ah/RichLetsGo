"""Explicit demo Trader entrypoint. No startup, credentials, or network on import."""

import argparse
import asyncio
import signal
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiohttp
import uvicorn

from richletsgo.backtest.codec import digest, encode, primitive
from richletsgo.config import AppConfig, load_config
from richletsgo.control.runtime import build_control
from richletsgo.domain import Environment
from richletsgo.exchange.bybit.gateway import BybitGateway
from richletsgo.exchange.bybit.rest import AiohttpTransport, RestClient
from richletsgo.exchange.bybit.streams import AiohttpSockets, StreamRunner
from richletsgo.health import install_health
from richletsgo.keystore import KeyStore, windows_backend
from richletsgo.live.inputs import BybitInputs
from richletsgo.live.trader import LoopPolicy, Trader
from richletsgo.notify.batching import WarningBatcher
from richletsgo.notify.bot import BotClient
from richletsgo.notify.events import Notice
from richletsgo.notify.telegram import Telegram
from richletsgo.oms.exposure import exposure
from richletsgo.oms.journal import ProtectiveJournal
from richletsgo.oms.synchronizer import Synchronizer, SyncPolicy
from richletsgo.ops.logging import Redactor, configure_logging
from richletsgo.portfolio.models import ClosePolicy
from richletsgo.portfolio.service import PortfolioService
from richletsgo.portfolio.signals import AiMode, AiScoreStore, SignalRouter
from richletsgo.risk.equity import AccountSnapshot, EquityTracker
from richletsgo.risk.gate import RiskGate
from richletsgo.risk.pause import PauseState
from richletsgo.storage.sqlite import SqliteDatabase, open_readonly


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class TraderServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The Trader owns shutdown: close entries before waiting for worker commits.
        yield


def loop_policy(path: Path) -> LoopPolicy:
    row = tomllib.loads(path.read_text(encoding="utf-8"))["trader"]
    names = (
        "tick_seconds",
        "bar_wait_seconds",
        "max_signal_age_seconds",
        "rest_every_seconds",
        "products_every_seconds",
        "warning_window_seconds",
    )
    if set(row) != set(names) or any(type(row[k]) is not int or row[k] <= 0 for k in names):
        raise ValueError("invalid Trader loop policy")
    return LoopPolicy(*(timedelta(seconds=row[k]) for k in names))


async def serve(config: AppConfig, policy: LoopPolicy, account_id: str, keys: KeyStore) -> None:
    if config.environment is not Environment.DEMO or config.ai_mode != "disabled":
        raise ValueError("M3 Trader only supports demo with AI disabled")
    clock = SystemClock()
    bot_token = keys.get(config.keystore["telegram_bot"])
    credential = keys.api(config.keystore["bybit_demo_api"], config.api_created_at)
    logger = configure_logging(
        config.paths.log_dir,
        environment=config.environment,
        service="Trader",
        retention_days=config.log_retention_days,
        secrets=(credential.key, credential.secret, bot_token),
    )

    warnings = WarningBatcher(
        clock, policy.warning_window, lambda message: logger.warning("%s", message)
    )

    telegram_holder: list[Telegram] = []

    def alert(message: str) -> None:
        if not isinstance(message, Notice):
            raise TypeError("alert producer must supply a structured Notice")
        cause = (config.environment.value, account_id, *message.key)
        if message.resolved:
            warnings.resolve(cause, message)
        else:
            warnings.warn(cause, message)
        if telegram_holder:
            telegram_holder[0].capture(message)

    def unique() -> str:
        return uuid4().hex

    database = SqliteDatabase(
        config.storage.db_file,
        account_id,
        config.environment,
        working_set_days=config.storage.working_set_days,
        busy_timeout_seconds=config.storage.busy_timeout_seconds,
        outcome_timeout_seconds=config.storage.outcome_timeout_seconds,
        alert=alert,
        now=clock.now,
    )
    # Startup verifies, never implicitly migrates or repairs a live database.
    async with database.unit_of_work():
        pass
    connection = open_readonly(database.path)
    try:
        if dict(connection.execute("SELECT key,value FROM meta")).get("account_id") != account_id:
            raise ValueError("Trader account metadata mismatch")
    finally:
        connection.close()
    pauses = PauseState()
    tracker = EquityTracker.open(
        AccountSnapshot(
            clock.now() - timedelta(days=config.storage.working_set_days), Decimal(0), Decimal(0)
        )
    )
    gate = RiskGate(
        account_id=account_id,
        environment=config.environment,
        uow_factory=database.unit_of_work,
        limits=config.risk,
        tracker=tracker,
        pauses=pauses,
        clock=clock,
        unique_factory=unique,
    )
    portfolio = PortfolioService(
        database.unit_of_work,
        clock,
        unique,
        ClosePolicy(
            config.control.cancel_max_attempts,
            config.control.cancel_max_attempts,
            timedelta(seconds=config.control.cancel_retry_seconds),
            config.bybit.trigger_by,
        ),
    )
    router = SignalRouter(
        gate,
        portfolio,
        AiScoreStore(),
        mode=AiMode.DISABLED,
        threshold=Decimal("0.5"),
        factor=lambda value: value,
        trigger_by=config.bybit.trigger_by,
    )
    record = {"config": config.public_dict(), "loop": primitive(policy)}
    config_record = encode({"hash": digest(record), **record})
    async with aiohttp.ClientSession() as session:
        rest = RestClient(config.bybit, credential, clock, AiohttpTransport(session), asyncio.sleep)
        gateway = BybitGateway(rest, pauses, alert)
        sync = Synchronizer(
            database.unit_of_work,
            gateway,
            portfolio,
            ProtectiveJournal(config.paths.journal_dir),
            clock,
            pauses,
            unique,
            SyncPolicy(
                timedelta(days=6, hours=23),
                timedelta(minutes=10),
                timedelta(minutes=2),
                timedelta(seconds=config.control.cancel_retry_seconds),
                3,
            ),
            config.bybit.symbols,
        )

        async def notify(message: str) -> None:
            await telegram.command_notification(message)

        control = build_control(config, database, gate, clock, keys, notify)
        telegram = Telegram(
            database=database,
            service=control.service,
            bot=BotClient(session, bot_token, config.telegram.http_timeout_seconds),
            clock=clock,
            policy=config.telegram,
            users=config.telegram_users,
            expires_at=config.api_expires_at,
            warning_window=policy.warning_window,
            redactor=Redactor((credential.key, credential.secret, bot_token)),
        )
        telegram_holder.append(telegram)
        inputs = BybitInputs(config, gateway, router)

        def status_values(u: Any) -> dict[str, Any]:
            if not set(config.bybit.symbols) <= inputs.marks.keys():
                return {"limit_usage": None}
            used = exposure(u, marks=inputs.marks, slippage_buffer=config.risk.slippage_buffer)
            return {"limit_usage": str(used.total_notional / config.risk.max_account_notional_usdt)}

        control.service.status_values = status_values

        async def recover() -> None:
            if not await database.recover(sync.synchronize):
                raise OSError("storage reconciliation pending")

        trader = Trader(
            uow=database.unit_of_work,
            clock=clock,
            gate=gate,
            portfolio=portfolio,
            synchronizer=sync,
            control=control.service,
            inputs=inputs,
            policy=policy,
            symbols=config.bybit.symbols,
            resolve_commit=database.resolve,
            recover=recover,
            config_record=config_record,
            alert=alert,
            initialize_tracker=True,
        )

        def watchdog() -> dict[str, Any]:
            return {
                **trader.health(),
                "last_commit": database.last_commit_at.isoformat()
                if database.last_commit_at
                else None,
                "telegram": dict(telegram.health),
            }

        install_health(control.api, watchdog)

        stop = asyncio.Event()
        server = TraderServer(
            uvicorn.Config(
                control.api,
                host="127.0.0.1",
                port=config.control.trader_port,
                access_log=False,
                proxy_headers=False,
            )
        )
        stream = StreamRunner(config.bybit, clock, AiohttpSockets(session), asyncio.sleep, alert)
        public_ready = False
        gate.entry_ready = lambda: trader.can_enter() and public_ready and gateway.connected
        public_task: asyncio.Task[None] | None = None

        async def public_state(connected: bool) -> None:
            nonlocal public_ready
            public_ready = connected
            if not connected:
                trader.ready = False
            elif gateway.connected and not trader.ready:
                await trader.boot()

        async def boot() -> bool:
            nonlocal public_task
            success = await trader.boot()
            if success and public_task is None:
                public_task = asyncio.create_task(stream.public(trader.accept, public_state))
            return success

        async def loop() -> None:
            while not stop.is_set():
                warnings.flush()
                try:
                    await trader.maintenance()
                    for event in tuple(sync.alerts):
                        alert(event)
                    sync.alerts.clear()
                except (OSError, ValueError):
                    trader.ready = False
                    alert(Notice("Trader reconciliation required", code="trader_reconcile"))
                await asyncio.sleep(policy.tick.total_seconds())

        async def boundaries() -> None:
            while not stop.is_set():
                if trader.ready and trader._boundaries:
                    try:
                        await trader.process_next()
                    except (OSError, ValueError):
                        trader.ready = False
                        alert(
                            Notice(
                                "Trader boundary reconciliation required", code="trader_boundary"
                            )
                        )
                await asyncio.sleep(policy.tick.total_seconds())

        await control.service.startup()  # Restore before private WS or any new account observation.
        if not await gateway.initialize():
            raise OSError("Bybit startup checks failed")
        alert(
            Notice(
                "Trader startup: " + config.environment.value + " AI=" + config.ai_mode,
                code="trader_started",
            )
        )
        telegram_task = asyncio.create_task(telegram.run(stop))
        tasks = [
            asyncio.create_task(stream.private(gateway, boot)),
            asyncio.create_task(loop()),
            asyncio.create_task(boundaries()),
            asyncio.create_task(gateway.maintain()),
            asyncio.create_task(server.serve()),
        ]

        def stopping(*_: object) -> None:
            stop.set()
            trader.stopping = True
            server.should_exit = True

        previous = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, stopping)
        stop_task = asyncio.create_task(stop.wait())
        try:
            completed, _ = await asyncio.wait(
                [*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
            )
            for task in completed:
                if task is not stop_task:
                    task.result()
                    raise OSError("Trader worker stopped unexpectedly")
        finally:
            stopping()
            for task in [*tasks, stop_task, *([public_task] if public_task else [])]:
                task.cancel()
            await asyncio.gather(
                *tasks, stop_task, *([public_task] if public_task else []), return_exceptions=True
            )
            telegram_task.cancel()
            await asyncio.gather(telegram_task, return_exceptions=True)
            await trader.close()
            await telegram.shutdown()
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--account", required=True)
    args = parser.parse_args()
    asyncio.run(
        serve(
            load_config(args.config),
            loop_policy(args.config),
            args.account,
            KeyStore(windows_backend()),
        )
    )


if __name__ == "__main__":
    main()
