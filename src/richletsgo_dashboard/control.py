"""Client side of the Trader control API (M5-01b). Until it exists, Trader is disconnected.

The dashboard only forwards commands; actor, environment check, confirmation and
recording all happen in the Trader's CommandService.
"""

import hashlib
import json
import threading
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from richletsgo.keystore import Secret

from .security import require_loopback

COMMANDS = ("pause", "resume", "cancel", "resolve")


class TraderClient(Protocol):
    def connected(self) -> bool: ...
    def command(
        self,
        command: str,
        *,
        session_id: str,
        environment: str,
        account_id: str,
        request_id: str,
        target: str | None = None,
        confirmation_id: str | None = None,
        manually_checked: bool = False,
    ) -> dict[str, Any]: ...
    def revoke(self, session_id: str) -> None: ...


class Disconnected:
    """Placeholder until M5-01b: every control is disabled, screens still render."""

    def connected(self) -> bool:
        return False

    def command(self, command: str, **kwargs: Any) -> dict[str, Any]:
        raise OSError("Trader unavailable")

    def revoke(self, session_id: str) -> None:
        pass


class HttpTraderClient:
    """Server-to-server client. Only hashed session IDs cross the process boundary."""

    def __init__(
        self,
        *,
        port: int,
        dashboard_port: int,
        token: Secret,
        environment: str,
        account_id: str,
        timeout_seconds: float = 3,
    ) -> None:
        require_loopback("127.0.0.1", port)
        require_loopback("127.0.0.1", dashboard_port)
        if not token.reveal() or timeout_seconds <= 0:
            raise ValueError("control token and positive timeout required")
        self.url, self.origin = f"http://127.0.0.1:{port}", f"http://127.0.0.1:{dashboard_port}"
        self.token, self.timeout = token, timeout_seconds
        self.environment, self.account_id = environment, account_id
        self._revoked: set[str] = set()
        self._lock = threading.RLock()

    @staticmethod
    def session_hash(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def _call(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = Request(
            self.url + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.token.reveal(),
                "Origin": self.origin,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(1_000_000)
                return dict(json.loads(raw)) if raw else {}
        except HTTPError as error:
            try:
                with error:
                    result = json.loads(error.read(1_000_000))
                if (
                    isinstance(result, dict)
                    and isinstance(result.get("state"), str)
                    and isinstance(result.get("message"), str)
                ):
                    return result
            except (ValueError, OSError):
                pass
            raise OSError("Trader control response unreadable") from None
        except (URLError, ValueError, OSError):
            raise OSError("Trader control request failed") from None

    def connected(self) -> bool:
        try:
            health = self._call("/health")
            return (
                health.get("environment") == self.environment
                and health.get("account_id") == self.account_id
            )
        except OSError:
            return False

    def _flush_revocations(self) -> None:
        for hashed in tuple(self._revoked):
            result = self._call("/sessions/revoke", {"session_id": hashed})
            if result.get("state") in ("rejected", "unavailable"):
                raise OSError("Trader session revocation not acknowledged")
            self._revoked.remove(hashed)

    def revoke(self, session_id: str) -> None:
        with self._lock:
            self._revoked.add(self.session_hash(session_id))
            try:
                self._flush_revocations()
            except OSError:
                pass  # retained for the next call; dashboard already invalidated the SID

    def command(
        self,
        command: str,
        *,
        session_id: str,
        environment: str,
        account_id: str,
        request_id: str,
        target: str | None = None,
        confirmation_id: str | None = None,
        manually_checked: bool = False,
    ) -> dict[str, Any]:
        if command not in COMMANDS:
            raise ValueError("unknown command")
        with self._lock:
            hashed = self.session_hash(session_id)
            if hashed in self._revoked:
                raise OSError("session revoked")
            self._flush_revocations()
            return self._call(
                "/commands/" + command,
                {
                    "session_id": hashed,
                    "environment": environment,
                    "account_id": account_id,
                    "request_id": request_id,
                    "target": target,
                    "confirmation_id": confirmation_id,
                    "manually_checked": manually_checked,
                },
            )
