"""Pure stop-preserving close state machine. No exchange or storage calls."""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from richletsgo.domain import StrategyId
from richletsgo.domain.clock import require_utc

from .models import Action, ActionKind, CloseIntent, ClosePolicy, CloseState


@dataclass(frozen=True, slots=True)
class StrategyView:
    strategy: StrategyId
    held: Decimal
    protected: Decimal
    pending_protection: bool
    outstanding_close: Decimal
    stops: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CloseView:
    strategies: tuple[StrategyView, ...]
    open_entries: tuple[str, ...]
    entries_settled: bool
    pending_fills: bool
    snapshot_revision: int


def advance_close(
    intent: CloseIntent, view: CloseView, now: datetime, policy: ClosePolicy
) -> tuple[CloseIntent, tuple[Action, ...]]:
    require_utc(now)
    own = next(v for v in view.strategies if v.strategy is intent.strategy)
    others = [v for v in view.strategies if v.strategy is not intent.strategy]
    actions: list[Action] = []

    def emit(
        kind: ActionKind,
        strategy: StrategyId = intent.strategy,
        qty: Decimal = Decimal(0),
        order: str | None = None,
        detail: str = "",
    ) -> None:
        key = f"{intent.intent_id}:{intent.state}:{kind}:{strategy}:{order}:{intent.close_attempts}:{intent.protect_attempts}:{view.snapshot_revision}"
        actions.append(Action(key, kind, strategy, intent.symbol, qty, order, detail))

    if intent.state is CloseState.DONE:
        return intent, ()
    for key in view.open_entries:
        emit(ActionKind.CANCEL_ENTRY, order=key)
    if intent.state is CloseState.RECORDED:
        intent = replace(intent, state=CloseState.CLOSING)
    # Late entry fills always return to CLOSING, even during final verification.
    if own.held or own.outstanding_close or not view.entries_settled or view.pending_fills:
        intent = replace(intent, state=CloseState.CLOSING)
    ready = intent.last_attempt_at is None or now - intent.last_attempt_at >= policy.retry_after
    if intent.state is CloseState.CLOSING:
        if (
            own.held == 0
            and not own.outstanding_close
            and view.entries_settled
            and not view.pending_fills
        ):
            return replace(
                intent, state=CloseState.FLAT_CONFIRMED, snapshot_revision=view.snapshot_revision
            ), tuple(actions)
        remaining = max(Decimal(0), abs(own.held) - own.outstanding_close)
        if remaining and ready:
            if intent.close_attempts >= policy.max_close_attempts:
                emit(ActionKind.PAUSE, detail="close retries exhausted; stops retained")
                emit(ActionKind.ALERT, detail="close retries exhausted; stops retained")
            else:
                emit(ActionKind.CLOSE, qty=-remaining if own.held > 0 else remaining)
                intent = replace(
                    intent, close_attempts=intent.close_attempts + 1, last_attempt_at=now
                )
        return intent, tuple(actions)
    if intent.state is CloseState.FLAT_CONFIRMED:
        emit(ActionKind.QUERY_STOPS)
        return replace(intent, state=CloseState.PROTECTING_OTHERS), tuple(actions)
    if intent.state is CloseState.PROTECTING_OTHERS:
        if view.snapshot_revision <= intent.snapshot_revision:
            emit(ActionKind.QUERY_STOPS)
            return intent, tuple(actions)
        counts = dict(intent.protect_attempts)
        safe = True
        for other in others:
            missing = abs(other.held) - other.protected
            if missing != 0 or other.pending_protection:
                safe = False
            if missing > 0 and ready:
                count = counts.get(other.strategy, 0)
                if count >= policy.max_protect_attempts:
                    uncovered = max(Decimal(0), missing - other.outstanding_close)
                    if uncovered:
                        emit(
                            ActionKind.REDUCE_UNPROTECTED,
                            other.strategy,
                            -uncovered if other.held > 0 else uncovered,
                        )
                    emit(
                        ActionKind.ALERT,
                        other.strategy,
                        detail="protection failed; owner stops retained",
                    )
                    emit(ActionKind.PAUSE, detail="protection recovery failed")
                elif not other.pending_protection:
                    emit(
                        ActionKind.PROTECT, other.strategy, -missing if other.held > 0 else missing
                    )
                counts[other.strategy] = count + 1
                intent = replace(intent, last_attempt_at=now)
            elif missing < 0:
                # Do not cancel whole stops here: caller must trim only confirmed excess.
                emit(ActionKind.TRIM_STOP, other.strategy, abs(other.held))
        if safe:
            return replace(intent, state=CloseState.STOPS_CANCELLING), tuple(actions)
        emit(ActionKind.QUERY_STOPS)
        return replace(intent, protect_attempts=tuple(sorted(counts.items()))), tuple(actions)
    if intent.state is CloseState.STOPS_CANCELLING:
        # Recheck others at every tick; a new mismatch returns to repair first.
        if any(v.protected != abs(v.held) or v.pending_protection for v in others):
            return replace(intent, state=CloseState.PROTECTING_OTHERS), tuple(actions)
        for key in own.stops:
            emit(ActionKind.CANCEL_STOP, order=key)
        emit(ActionKind.QUERY_STOPS)
        return replace(
            intent, state=CloseState.VERIFYING, snapshot_revision=view.snapshot_revision
        ), tuple(actions)
    if intent.state is CloseState.VERIFYING:
        if view.snapshot_revision <= intent.snapshot_revision:
            emit(ActionKind.QUERY_STOPS)
        elif any(v.protected != abs(v.held) or v.pending_protection for v in others):
            return replace(intent, state=CloseState.PROTECTING_OTHERS), tuple(actions)
        elif own.stops or own.pending_protection:
            return replace(intent, state=CloseState.STOPS_CANCELLING), tuple(actions)
        else:
            return replace(intent, state=CloseState.DONE), tuple(actions)
    return intent, tuple(actions)
