"""Account-independent signals and immutable shared AI scores (D43/D56)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from richletsgo.domain import Price, StrategyId, Symbol
from richletsgo.domain.values import finite_decimal
from richletsgo.risk.checks import StrategyRiskInput
from richletsgo.risk.gate import Admission, EntryRequest, MarketInputs, RiskGate
from richletsgo.risk.records import Direction, Rejection, RejectionCode, signal_key
from richletsgo.storage.contracts import UnitOfWork

from .service import PortfolioService


class AiMode(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class ScoreStatus(StrEnum):
    OK = "OK"
    TIMEOUT = "TIMEOUT"
    INVALID_FORMAT = "INVALID_FORMAT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    STALE_PERIODIC = "STALE_PERIODIC"


@dataclass(frozen=True, slots=True)
class AiScore:
    status: ScoreStatus
    score: Decimal | None = None
    direction_agrees: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.status, ScoreStatus) or not isinstance(self.direction_agrees, bool):
            raise TypeError("invalid score status/direction agreement")
        if self.status is ScoreStatus.OK:
            if self.score is None or not 0 <= finite_decimal(self.score, "score") <= 1:
                raise ValueError("OK requires a score in [0,1]")
        elif self.score is not None:
            raise ValueError("failed evaluation cannot carry a score")


class AiScoreStore:
    def __init__(self) -> None:
        self._scores: dict[str, AiScore] = {}

    def put(self, signal_id: str, result: AiScore) -> None:
        existing = self._scores.get(signal_id)
        if existing is not None and existing != result:
            raise ValueError("signal score is immutable")
        self._scores[signal_id] = result

    def get(self, signal_id: str) -> AiScore | None:
        return self._scores.get(signal_id)


@dataclass(frozen=True, slots=True)
class MarketSignal:
    strategy: StrategyId
    symbol: Symbol
    bar_start: datetime
    kind: str
    direction: Direction
    entry_price: Price
    stop_price: Price
    limit_price: Price | None = None

    def __post_init__(self) -> None:
        signal_key(self.strategy, self.symbol, self.bar_start, self.kind)
        if self.kind not in ("ENTRY", "EXIT"):
            raise ValueError("signal kind must be ENTRY or EXIT")
        if not isinstance(self.stop_price, Price) or not isinstance(self.entry_price, Price):
            raise TypeError("signal requires entry and stop prices")

    @property
    def signal_id(self) -> str:
        return signal_key(self.strategy, self.symbol, self.bar_start, self.kind)


class SignalRouter:
    def __init__(
        self,
        gate: RiskGate,
        portfolio: PortfolioService,
        scores: AiScoreStore,
        *,
        mode: AiMode,
        threshold: Decimal,
        factor: Callable[[Decimal], Decimal],
        trigger_by: str,
    ) -> None:
        if not isinstance(mode, AiMode):
            raise TypeError("mode must be AiMode")
        if not 0 <= finite_decimal(threshold, "threshold") <= 1:
            raise ValueError("invalid AI threshold")
        self.gate, self.portfolio, self.scores = gate, portfolio, scores
        self.mode, self.threshold, self.factor, self.trigger_by = (
            mode,
            threshold,
            factor,
            trigger_by,
        )

    async def handle(
        self,
        signal: MarketSignal,
        market: MarketInputs,
        strategies: Mapping[StrategyId, StrategyRiskInput],
    ) -> Admission:
        score = self.scores.get(signal.signal_id)
        rejection = None
        factor = Decimal(1)
        if self.mode is AiMode.ENABLED:
            if score is None or score.status is not ScoreStatus.OK:
                rejection = Rejection(
                    RejectionCode.AI_BLOCKED, score.status.value if score else "MISSING"
                )
            elif not score.direction_agrees or score.score is None or score.score < self.threshold:
                rejection = Rejection(RejectionCode.AI_BLOCKED, "threshold/direction")
            else:
                factor = self.factor(score.score)
                if not Decimal("0.5") <= finite_decimal(factor, "factor") <= 1:
                    raise ValueError("AI factor must be in [0.5,1]")
        request = EntryRequest(
            signal.signal_id,
            signal.strategy,
            signal.symbol,
            signal.direction,
            signal.entry_price,
            signal.stop_price,
            factor,
            signal.limit_price,
            stop_trigger_by=self.trigger_by,
            ai_mode=self.mode.value,
            ai_score=score.score if score else None,
            ai_status=score.status.value if score else "MISSING",
            entry_rejection=rejection,
            is_exit=signal.kind == "EXIT",
        )

        def after(uow: UnitOfWork, admission: Admission) -> None:
            episode = admission.episode
            if episode and (signal.kind == "EXIT" or episode.direction is not signal.direction):
                self.portfolio.request_close(uow, episode)
            self.portfolio.reconcile(uow)

        return await self.gate.admit(
            request, market, strategies, prepare=self.portfolio.reconcile, after_decision=after
        )
