"""Supervised WS sessions. Reader starts before synchronization and buffers its events."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

import aiohttp

from richletsgo.config import BybitConfig
from richletsgo.domain.clock import Clock
from richletsgo.keystore import ApiCredential
from richletsgo.marketdata.models import Bar, to_ms

from .gateway import BybitGateway
from .mapping import bars
from .rest import signature


class Socket(Protocol):
    async def send(self, data: dict[str, Any]) -> None: ...
    async def receive(self) -> dict[str, Any]: ...


class SocketFactory(Protocol):
    def connect(self, url: str) -> AbstractAsyncContextManager[Socket]: ...


class AiohttpSocket:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws

    async def send(self, data: dict[str, Any]) -> None:
        await self.ws.send_json(data)

    async def receive(self) -> dict[str, Any]:
        message = await self.ws.receive()
        if message.type is not aiohttp.WSMsgType.TEXT:
            raise ConnectionError("Bybit WS closed or invalid frame")
        data = message.json()
        if not isinstance(data, dict):
            raise ValueError("Bybit WS object required")
        return dict(data)


class AiohttpSockets:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    @asynccontextmanager
    async def connect(self, url: str) -> AsyncIterator[Socket]:
        async with self.session.ws_connect(url, autoping=True) as ws:
            yield AiohttpSocket(ws)


class StreamRunner:
    def __init__(
        self,
        config: BybitConfig,
        clock: Clock,
        factory: SocketFactory,
        sleep: Callable[[float], Awaitable[None]],
        alert: Callable[[str], None],
    ) -> None:
        if config.private_ws_url != "wss://stream-demo.bybit.com/v5/private" or (
            config.public_ws_url != "wss://stream.bybit.com/v5/public/linear"
        ):
            raise ValueError("demo private/mainnet public WS required")
        if (
            not 1 <= config.ws_ping_seconds <= 20
            or config.ws_backoff_seconds < 1
            or (config.ws_max_backoff_seconds < config.ws_backoff_seconds)
        ):
            raise ValueError("invalid WS pacing")
        self.config, self.clock, self.factory = config, clock, factory
        self.sleep, self.alert = sleep, alert

    async def _session(
        self,
        ws: Socket,
        topics: list[str],
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        on_ready: Callable[[], Awaitable[None]],
        credential: ApiCredential | None,
    ) -> None:
        pending: list[dict[str, Any]] = []

        async def acknowledge(op: str) -> None:
            async with asyncio.timeout(self.config.timeout_seconds):
                while True:
                    message = await ws.receive()
                    if message.get("op") == op:
                        if message.get("success") is not True:
                            raise ConnectionError("Bybit WS handshake rejected")
                        return
                    if "topic" in message:
                        pending.append(message)

        if credential is not None:
            expires = to_ms(self.clock.now()) + self.config.recv_window_ms
            await ws.send(
                {
                    "op": "auth",
                    "args": [
                        credential.key.reveal(),
                        expires,
                        signature(credential.secret.reveal(), f"GET/realtime{expires}"),
                    ],
                }
            )
            await acknowledge("auth")
        await ws.send({"op": "subscribe", "args": topics})
        await acknowledge("subscribe")
        pong = asyncio.Event()

        async def reader() -> None:
            for message in pending:
                await on_message(message)
            while True:
                message = await ws.receive()
                if message.get("op") == "pong" or (
                    message.get("op") == "ping" and message.get("ret_msg") == "pong"
                ):
                    pong.set()
                elif "topic" in message:
                    await on_message(message)
                elif message.get("success") is False:
                    raise ConnectionError("Bybit WS operation rejected")

        async def heartbeat() -> None:
            while True:
                await self.sleep(self.config.ws_ping_seconds)
                pong.clear()
                await ws.send({"op": "ping"})
                await asyncio.wait_for(pong.wait(), timeout=self.config.ws_ping_seconds)

        async with asyncio.TaskGroup() as group:
            group.create_task(reader())
            group.create_task(heartbeat())
            await on_ready()  # reader can deliver events while M1-06 performs REST reads

    async def private(
        self, gateway: BybitGateway, synchronize: Callable[[], Awaitable[bool]]
    ) -> None:
        delay = self.config.ws_backoff_seconds

        async def ready() -> None:
            gateway.stream_state(True)
            await gateway.check_settings()
            await gateway.check_clock()
            success = await synchronize()
            gateway.synchronized(success)
            if not success:
                raise ConnectionError("Bybit reconciliation incomplete")

        while True:
            try:
                async with self.factory.connect(self.config.private_ws_url) as ws:
                    await self._session(
                        ws,
                        ["order", "execution", "position", "wallet"],
                        gateway.on_private_message,
                        ready,
                        gateway.rest.credential,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.alert("bybit_private_reconnect")  # never expose authentication payload
            finally:
                gateway.stream_state(False)
            # At least one second even after a successful short-lived connection: <500 / 5min.
            await self.sleep(delay)
            delay = min(delay * 2, self.config.ws_max_backoff_seconds)

    async def public(
        self,
        on_bar: Callable[[Bar], Awaitable[None]],
        on_connection: Callable[[bool], Awaitable[None]],
    ) -> None:
        delay = self.config.ws_backoff_seconds

        async def message(payload: dict[str, Any]) -> None:
            for bar in bars(payload):
                await on_bar(bar)

        async def ready() -> None:
            await on_connection(True)

        topics = [
            f"kline.{interval}.{symbol.value}"
            for symbol in self.config.symbols
            for interval in ("60", "D")
        ]
        while True:
            try:
                async with self.factory.connect(self.config.public_ws_url) as ws:
                    await self._session(ws, topics, message, ready, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.alert("bybit_public_reconnect")
            finally:
                await on_connection(False)
            await self.sleep(delay)
            delay = min(delay * 2, self.config.ws_max_backoff_seconds)
