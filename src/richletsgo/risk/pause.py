"""Entry pause states (02 5장). Pauses never block reducing or closing orders."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from richletsgo.domain import StrategyId, Symbol
from richletsgo.domain.clock import require_utc


class PauseKind(StrEnum):
    STRATEGY_PAUSED = "STRATEGY_PAUSED"
    ENTRY_PAUSED_DAILY = "ENTRY_PAUSED_DAILY"
    ENTRY_PAUSED_SYSTEM = "ENTRY_PAUSED_SYSTEM"
    ENTRY_PAUSED_MANUAL = "ENTRY_PAUSED_MANUAL"
    HALTED = "HALTED"


type PauseKey = tuple[PauseKind, str, StrategyId | None, Symbol | None]


@dataclass(frozen=True, slots=True)
class Pause:
    kind: PauseKind
    reason: str
    since: datetime
    strategy: StrategyId | None = None
    symbol: Symbol | None = None
    until: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PauseKind):
            raise TypeError("kind must be PauseKind")
        if not self.reason or not self.reason.strip():
            raise ValueError("reason is required")
        require_utc(self.since)
        if self.until is not None and require_utc(self.until) <= self.since:
            raise ValueError("until must be after since")
        strategy_kind = self.kind is PauseKind.STRATEGY_PAUSED
        if strategy_kind != (self.strategy is not None):
            raise ValueError("only STRATEGY_PAUSED names a strategy")
        if self.symbol is not None and self.kind is not PauseKind.ENTRY_PAUSED_SYSTEM:
            raise ValueError("only ENTRY_PAUSED_SYSTEM may be limited to a symbol")
        timed = self.kind in (PauseKind.STRATEGY_PAUSED, PauseKind.ENTRY_PAUSED_DAILY)
        if timed != (self.until is not None):
            raise ValueError("STRATEGY_PAUSED and ENTRY_PAUSED_DAILY need until; others none")

    @property
    def key(self) -> PauseKey:
        return (self.kind, self.reason, self.strategy, self.symbol)

    def blocks(self, strategy: StrategyId, symbol: Symbol) -> bool:
        if self.kind is PauseKind.STRATEGY_PAUSED:
            return self.strategy is strategy
        if self.kind is PauseKind.ENTRY_PAUSED_SYSTEM:
            return self.symbol is None or self.symbol is symbol
        return True


class PauseState:
    """In-memory set of active pauses; persistence belongs to M1-06/M3 via to/from_json."""

    def __init__(self, pauses: tuple[Pause, ...] = ()) -> None:
        self._pauses: dict[PauseKey, Pause] = {}
        self._revision = 0
        for pause in pauses:
            self.add(pause)

    @property
    def revision(self) -> int:
        """Changes whenever the active pause set changes, including remove/re-add."""
        return self._revision

    def add(self, pause: Pause) -> bool:
        """Return True when newly added; an existing timed pause only extends its until."""
        current = self._pauses.get(pause.key)
        if current is None:
            self._pauses[pause.key] = pause
            self._revision += 1
            return True
        if pause.until is not None and current.until is not None and pause.until > current.until:
            self._revision += 1
            self._pauses[pause.key] = Pause(
                current.kind,
                current.reason,
                current.since,
                current.strategy,
                current.symbol,
                pause.until,
            )
        return False

    def clear(
        self,
        kind: PauseKind,
        *,
        reason: str | None = None,
        strategy: StrategyId | None = None,
        symbol: Symbol | None = None,
    ) -> tuple[Pause, ...]:
        removed = tuple(
            p
            for p in self._pauses.values()
            if p.kind is kind
            and (reason is None or p.reason == reason)
            and (strategy is None or p.strategy is strategy)
            and (symbol is None or p.symbol is symbol)
        )
        for pause in removed:
            del self._pauses[pause.key]
        if removed:
            self._revision += 1
        return removed

    def expire(self, now: datetime) -> tuple[Pause, ...]:
        """Release timed pauses (next trading day for L4/L5, cooldown end for L7)."""
        require_utc(now)
        removed = tuple(p for p in self._pauses.values() if p.until is not None and p.until <= now)
        for pause in removed:
            del self._pauses[pause.key]
        if removed:
            self._revision += 1
        return removed

    def blocking(self, strategy: StrategyId, symbol: Symbol) -> tuple[Pause, ...]:
        return tuple(p for p in self.active() if p.blocks(strategy, symbol))

    def has(self, kind: PauseKind) -> bool:
        return any(p.kind is kind for p in self._pauses.values())

    def active(self) -> tuple[Pause, ...]:
        return tuple(sorted(self._pauses.values(), key=lambda p: (p.since, p.key[0], p.reason)))

    def to_json(self) -> list[dict[str, Any]]:
        return [
            {
                "kind": p.kind.value,
                "reason": p.reason,
                "since": p.since.isoformat(),
                "strategy": p.strategy.value if p.strategy else None,
                "symbol": p.symbol.value if p.symbol else None,
                "until": p.until.isoformat() if p.until else None,
            }
            for p in self.active()
        ]

    @classmethod
    def from_json(cls, data: list[dict[str, Any]]) -> "PauseState":
        return cls(
            tuple(
                Pause(
                    PauseKind(item["kind"]),
                    item["reason"],
                    datetime.fromisoformat(item["since"]),
                    StrategyId(item["strategy"]) if item["strategy"] else None,
                    Symbol(item["symbol"]) if item["symbol"] else None,
                    datetime.fromisoformat(item["until"]) if item["until"] else None,
                )
                for item in data
            )
        )
