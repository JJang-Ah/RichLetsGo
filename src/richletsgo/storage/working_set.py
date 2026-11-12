"""Shared retention semantics; references always win over the recent-history cutoff."""

from datetime import datetime

from richletsgo.portfolio.models import CloseState
from richletsgo.risk.records import EpisodeState

from .contracts import FillStatus
from .memory import _State


def signal_time(signal_id: str) -> datetime | None:
    parts = signal_id.split("|")
    if len(parts) != 4:
        return None  # Legacy fixture identifiers have no candle timestamp.
    return datetime.fromisoformat(parts[2])


def working_set(state: _State, cutoff: datetime) -> _State:
    result = state.clone()
    active_ids = {i.episode_id for i in state.intents.values() if i.state is not CloseState.DONE}
    result.episodes = {
        k: e
        for k, e in state.episodes.items()
        if e.state is not EpisodeState.CLOSED or k in active_ids or e.opened_at >= cutoff
    }
    references = {k for e in result.episodes.values() for k in e.entry_order_keys}
    references.update(s.order_key for b in state.books.values() for s in b.stops)
    references.update(a.order_key for a in state.actions.values() if a.order_key)
    recovering = set(state.recoveries)
    result.fills = {
        k: f
        for k, f in state.fills.items()
        if f.status is not FillStatus.BOOKED or f.event.exec_time >= cutoff
    }
    references.update(f.order_key for f in result.fills.values() if f.order_key)
    result.orders = {
        k: o
        for k, o in state.orders.items()
        if not o.state.is_terminal
        or o.unbooked_qty
        or o.updated_at >= cutoff
        or k in references
        or (o.strategy, o.symbol) in recovering
    }
    result.decisions = {
        k: d for k, d in state.decisions.items() if (signal_time(k) or d.decided_at) >= cutoff
    }
    result.intents = {
        k: i
        for k, i in state.intents.items()
        if i.state is not CloseState.DONE or i.created_at >= cutoff
    }
    return result
