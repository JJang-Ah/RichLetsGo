"""Server-created command identity and two-step confirmation records."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from richletsgo.domain import Environment
from richletsgo.risk.resume import ResumePrompt

COMMANDS = frozenset({"pause", "resume", "cancel", "resolve"})


@dataclass(frozen=True)
class Context:
    source: str
    actor_id: str
    session_id: str
    environment: Environment
    account_id: str
    request_id: str

    def __post_init__(self) -> None:
        if self.source not in ("telegram", "web") or not self.actor_id.startswith(
            self.source + ":"
        ):
            raise ValueError("invalid command source/actor")
        if any(
            not v or len(v) > 256
            for v in (self.actor_id, self.session_id, self.account_id, self.request_id)
        ):
            raise ValueError("invalid command context")

    def identity(self) -> dict[str, str]:
        return {
            "source": self.source,
            "actor_id": self.actor_id,
            "session_id": self.session_id,
            "environment": self.environment.value,
            "account_id": self.account_id,
            "request_id": self.request_id,
        }


@dataclass(frozen=True)
class CommandPolicy:
    cancel_retry_after: timedelta
    cancel_max_attempts: int

    def __post_init__(self) -> None:
        if self.cancel_retry_after <= timedelta(0) or self.cancel_max_attempts < 1:
            raise ValueError("positive cancel interval/attempt limit required")


@dataclass
class Confirmation:
    confirmation_id: str
    context: Context
    command: str
    target: str | None
    expires_at: datetime
    targets: tuple[str, ...] = ()
    version: tuple[str, int] | None = None
    resume: ResumePrompt | None = None
    result: dict[str, Any] | None = None
