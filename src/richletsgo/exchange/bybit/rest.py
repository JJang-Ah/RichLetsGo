"""Signed REST transport. Never repeat a create or amend request after an ambiguous outcome."""

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlencode

import aiohttp

from richletsgo.config import BybitConfig
from richletsgo.domain.clock import Clock
from richletsgo.exchange.contracts import ErrorCode, ExchangeError
from richletsgo.keystore import ApiCredential
from richletsgo.marketdata.models import to_ms


class Transport(Protocol):
    async def request(
        self, method: str, url: str, headers: Mapping[str, str], body: str | None, timeout: float
    ) -> tuple[int, bytes]: ...


class AiohttpTransport:
    """Session lifetime is owned by the caller. No redirects of signed headers."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def request(
        self, method: str, url: str, headers: Mapping[str, str], body: str | None, timeout: float
    ) -> tuple[int, bytes]:
        try:
            async with self.session.request(
                method,
                url,
                headers=headers,
                data=body.encode() if body is not None else None,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as response:
                return response.status, await response.read()
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise ConnectionError("Bybit transport unavailable") from None


def signature(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def api_error(code: int, *, query: bool, amend: bool) -> ExchangeError:
    # A duplicate can be the earlier accepted request. Resolve by orderLinkId, never reject it.
    if code in (10000, 10016, 10019, 10006, 10429, 20006, 10014, 110072):
        kind = ErrorCode.QUERY_FAILED if query else ErrorCode.TIMEOUT
    elif query:
        kind = ErrorCode.QUERY_FAILED
    elif code == 110001:
        kind = ErrorCode.NOT_FOUND
    elif code in (110008, 110010):
        kind = ErrorCode.TOO_LATE
    elif amend and code in (110059, 110060):
        kind = ErrorCode.UNSUPPORTED
    else:
        kind = ErrorCode.REJECTED
    return ExchangeError(kind, f"Bybit retCode={code}")  # never echo retMsg/request/secrets


class RestClient:
    def __init__(
        self,
        config: BybitConfig,
        credential: ApiCredential,
        clock: Clock,
        transport: Transport,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        if config.rest_url != "https://api-demo.bybit.com":
            raise ValueError("demo REST endpoint required")
        if config.public_rest_url != "https://api.bybit.com":
            raise ValueError("mainnet public endpoint required")
        self.config, self.credential, self.clock = config, credential, clock
        self.transport, self.sleep = transport, sleep
        self.clock_offset_ms: int = 0

    async def call(
        self, method: str, path: str, params: Mapping[str, Any], *, public: bool = False
    ) -> dict[str, Any] | ExchangeError:
        single_attempt = path in ("/v5/order/create", "/v5/order/amend")
        query = method == "GET"
        serialized = (
            urlencode(sorted(params.items()))
            if query
            else json.dumps(params, separators=(",", ":"), ensure_ascii=True, sort_keys=True)
        )
        attempts = 1 if single_attempt else self.config.retry_attempts
        failure = ExchangeError(
            ErrorCode.QUERY_FAILED if query else ErrorCode.TIMEOUT, "Bybit response unavailable"
        )
        for attempt in range(attempts):
            if attempt:
                await self.sleep(self.config.retry_delay_seconds * 2 ** (attempt - 1))
            headers = {"Content-Type": "application/json"}
            if not public:
                timestamp = str(to_ms(self.clock.now()) + self.clock_offset_ms)
                key = self.credential.key.reveal()
                window = str(self.config.recv_window_ms)
                headers.update(
                    {
                        "X-BAPI-API-KEY": key,
                        "X-BAPI-TIMESTAMP": timestamp,
                        "X-BAPI-RECV-WINDOW": window,
                        "X-BAPI-SIGN": signature(
                            self.credential.secret.reveal(), timestamp + key + window + serialized
                        ),
                    }
                )
            base = self.config.public_rest_url if public else self.config.rest_url
            url = base + path + ("?" + serialized if query and serialized else "")
            try:
                status, raw = await self.transport.request(
                    method, url, headers, None if query else serialized, self.config.timeout_seconds
                )
            except (ConnectionError, TimeoutError, OSError):
                continue
            if status == 429 or status >= 500:
                continue
            if status != 200:
                return ExchangeError(
                    ErrorCode.QUERY_FAILED if query else ErrorCode.REJECTED, f"Bybit HTTP {status}"
                )
            try:
                data = json.loads(raw)
                code = data["retCode"]
                if type(code) is not int:
                    raise ValueError
                if code:
                    failure = api_error(code, query=query, amend=path == "/v5/order/amend")
                    if code in (10006, 10429):
                        continue
                    return failure
                if not isinstance(data["result"], dict):
                    raise ValueError
                return dict(data)
            except (KeyError, ValueError, TypeError):
                # Malformed responses can hide successful writes; do not repeat them.
                return failure
        return failure

    async def pages(
        self, path: str, params: Mapping[str, Any], *, public: bool = False
    ) -> list[dict[str, Any]] | ExchangeError:
        rows: list[dict[str, Any]] = []
        cursor = ""
        seen: set[str] = set()
        while True:
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            payload = await self.call("GET", path, query, public=public)
            if isinstance(payload, ExchangeError):
                return payload
            result = payload["result"]
            page = result.get("list")
            if not isinstance(page, list) or any(not isinstance(r, dict) for r in page):
                return ExchangeError(ErrorCode.QUERY_FAILED, "Bybit invalid page")
            rows.extend(page)
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                return rows
            if not isinstance(cursor, str) or cursor in seen:
                return ExchangeError(ErrorCode.QUERY_FAILED, "Bybit repeated cursor")
            seen.add(cursor)
