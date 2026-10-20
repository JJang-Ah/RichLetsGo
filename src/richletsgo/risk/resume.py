"""Two-step /resume (D48, 02 5.1절). There is no resume path without confirmation."""

from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from richletsgo.domain.clock import require_utc

from .equity import EquityTracker, PeakReset
from .guards import CommandAuthorization, authorize_command
from .limits import RiskLimits
from .pause import PauseKind, PauseState

RESUME_COMMAND = "/resume"


@dataclass(frozen=True, slots=True)
class ResumePrompt:
    """What the user is asked to confirm; values are shown verbatim in the bot message."""

    prompt_id: str
    actor_id: str
    created_at: datetime
    expires_at: datetime
    clears_manual: bool
    clears_halted: bool
    record_peak: Decimal
    risk_peak: Decimal
    equity: Decimal
    drawdown_ratio: Decimal
    new_risk_peak: Decimal | None
    next_halt_equity: Decimal | None


@dataclass(frozen=True, slots=True)
class ResumeRecord:
    prompt_id: str
    actor_id: str
    confirmed_at: datetime
    cleared: tuple[PauseKind, ...]
    peak_reset: PeakReset | None


@dataclass(frozen=True, slots=True)
class ResumeRejected:
    reason: str
    authorization: CommandAuthorization


class ResumeService:
    def __init__(
        self,
        *,
        pauses: PauseState,
        tracker: EquityTracker,
        limits: RiskLimits,
        allowed_ids: Collection[str],
        ttl: timedelta,
        id_factory: Callable[[], str],
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self._pauses = pauses
        self._tracker = tracker
        self._limits = limits
        self._allowed = frozenset(allowed_ids)
        self._ttl = ttl
        self._ids = id_factory
        self._prompts: dict[str, ResumePrompt] = {}

    def request_resume(self, actor_id: str, at: datetime) -> ResumePrompt | ResumeRejected:
        auth = authorize_command(actor_id, RESUME_COMMAND, self._allowed, at)
        if not auth.allowed:
            return ResumeRejected("unauthorized", auth)
        halted = self._pauses.has(PauseKind.HALTED)
        manual = self._pauses.has(PauseKind.ENTRY_PAUSED_MANUAL)
        if not (halted or manual):
            return ResumeRejected("nothing to resume", auth)
        equity = self._tracker.equity
        new_peak = equity if halted else None
        prompt = ResumePrompt(
            self._ids(),
            actor_id,
            at,
            at + self._ttl,
            manual,
            halted,
            self._tracker.record_peak,
            self._tracker.risk_peak,
            equity,
            self._tracker.drawdown_ratio,
            new_peak,
            new_peak * (1 - self._limits.max_drawdown_ratio) if new_peak is not None else None,
        )
        self._prompts[prompt.prompt_id] = prompt
        return prompt

    def confirm_resume(
        self, prompt_id: str, actor_id: str, at: datetime
    ) -> ResumeRecord | ResumeRejected:
        auth = authorize_command(actor_id, RESUME_COMMAND, self._allowed, require_utc(at))
        if not auth.allowed:
            return ResumeRejected("unauthorized", auth)
        prompt = self._prompts.pop(prompt_id, None)
        if prompt is None:
            return ResumeRejected("unknown prompt", auth)
        if prompt.actor_id != actor_id:
            return ResumeRejected("prompt belongs to another actor", auth)
        if at > prompt.expires_at:
            return ResumeRejected("prompt expired", auth)
        cleared: list[PauseKind] = []
        reset: PeakReset | None = None
        if prompt.clears_halted and self._pauses.clear(PauseKind.HALTED):
            assert prompt.new_risk_peak is not None
            reset = self._tracker.reset_risk_peak(prompt.new_risk_peak, at, actor_id)
            cleared.append(PauseKind.HALTED)
        if prompt.clears_manual and self._pauses.clear(PauseKind.ENTRY_PAUSED_MANUAL):
            cleared.append(PauseKind.ENTRY_PAUSED_MANUAL)
        return ResumeRecord(prompt_id, actor_id, at, tuple(cleared), reset)
