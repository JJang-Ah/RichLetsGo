"""Small Bot API transport. Never expose token-bearing URLs or remote error bodies."""

from typing import Any, Protocol

import aiohttp

from richletsgo.keystore import Secret


class BotError(OSError):
    def __init__(self, code: int = 0, retry_after: int = 0) -> None:
        self.code, self.retry_after = code, max(0, retry_after)
        super().__init__(f"Telegram unavailable ({code})")


class Bot(Protocol):
    async def call(self, method: str, payload: dict[str, Any]) -> Any: ...


class BotClient:
    def __init__(self, session: aiohttp.ClientSession, token: Secret, timeout_seconds: int) -> None:
        self.session, self._token = session, token
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        if method not in {"getMe", "getUpdates", "sendMessage", "answerCallbackQuery"}:
            raise ValueError("unsupported Bot API method")
        try:
            async with self.session.post(
                "https://api.telegram.org/bot" + self._token.reveal() + "/" + method,
                json=payload,
                timeout=self.timeout,
                allow_redirects=False,
            ) as response:
                body = await response.json()
                if not isinstance(body, dict):
                    raise BotError()
                if response.status != 200 or body.get("ok") is not True:
                    params = body.get("parameters", {})
                    retry = params.get("retry_after", 0) if isinstance(params, dict) else 0
                    code = body.get("error_code", response.status)
                    raise BotError(
                        code if type(code) is int else response.status,
                        retry if type(retry) is int else 0,
                    )
                return body["result"]
        except BotError:
            raise
        except Exception:
            raise BotError() from None
