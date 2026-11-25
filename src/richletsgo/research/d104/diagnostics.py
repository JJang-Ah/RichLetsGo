"""Price-free request diagnostics for the D104 collector."""

import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from http.client import HTTPException
from typing import Any


class HttpStatusError(OSError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"HTTP {status}")


@dataclass
class Attempt:
    category: str = "format"
    received_rows: int | None = None
    received_bars: int | None = None
    missing_utc: list[str] | None = None
    code: int | None = None

    def request(self, get: Callable[[str], bytes], path: str) -> bytes | None:
        try:
            return get(path)
        except HttpStatusError as exc:
            self.category, self.code = "exchange", exc.status
        except (OSError, HTTPException):
            self.category = "transport"
        return None

    def result(self, raw: bytes) -> Any:
        msg = json.loads(raw)
        code = msg["retCode"]
        if code != 0:
            if type(code) is int:
                self.category, self.code = "exchange", code
            raise ValueError("invalid envelope")
        result = msg["result"]
        if isinstance(result["list"], list):
            self.received_rows = len(result["list"])
        return result

    def missing(self, expected: range, received: Iterable[int]) -> None:
        received_keys = set(received)
        self.received_bars = len(received_keys)
        self.category = "missing_bars"
        self.missing_utc = [
            datetime.fromtimestamp(at // 1000, UTC).isoformat()
            for at in sorted(set(expected).difference(received_keys))
        ]


class CollectionFailure(ValueError):
    def __init__(
        self,
        kind: str,
        symbol: str,
        start: int,
        end: int,
        expected: int | None,
        attempts: list[Attempt],
    ):
        self.record = dict(
            failed_at=datetime.now(UTC).isoformat(),
            kind=kind,
            symbol=symbol,
            start_ms=start,
            end_exclusive_ms=end,
            expected_rows=expected,
            attempts=[dict(attempt=i, **asdict(a)) for i, a in enumerate(attempts, 1)],
        )
        prefix = (
            "INVALID_INPUT incomplete"
            if attempts[-1].category == "missing_bars"
            else "INVALID_INPUT"
        )
        super().__init__(f"{prefix} {json.dumps(self.record, separators=(',', ':'))}")
