"""Clock-driven warning episodes, shared by logging and notification delivery."""

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Any

from richletsgo.domain import Clock

from .events import Notice


@dataclass
class _Episode:
    start: datetime
    last: datetime
    message: str
    repeated: int = 0


class WarningBatcher:
    """First occurrence immediately, then one repeated-count summary per fixed window.

    Callers supply stable reason identities, never prices/timestamps as identities.
    This local component makes no I/O and does not replace a durable Bot outbox.
    """

    def __init__(self, clock: Clock, window: timedelta, emit: Callable[[str], None]) -> None:
        if window <= timedelta(0):
            raise ValueError("positive warning window required")
        self.clock, self.window, self.emit = clock, window, emit
        self._lock = RLock()
        self._episodes: dict[Hashable, _Episode] = {}

    def warn(self, key: Hashable, message: str) -> None:
        with self._lock:
            self.flush()
            now = self.clock.now()
            episode = self._episodes.get(key)
            if episode is None:
                self.emit(message)
                self._episodes[key] = _Episode(now, now, message)
            else:
                episode.last, episode.message = now, message
                episode.repeated += 1

    def flush(self) -> None:
        with self._lock:
            now = self.clock.now()
            for key, episode in tuple(self._episodes.items()):
                if now - episode.start >= self.window:
                    self._summary(episode)
                    if episode.repeated:
                        episode.start += ((now - episode.start) // self.window) * self.window
                        episode.repeated = 0
                    else:
                        self._episodes.pop(key)

    def resolve(self, key: Hashable, message: str) -> None:
        with self._lock:
            episode = self._episodes.pop(key, None)
            if episode is not None:
                self._summary(episode)
            self.emit(message)

    def transition(self, key: Hashable, message: str) -> None:
        """Coalesce on/off changes without resetting the cause's fixed window."""
        self.warn(key, message)

    def _summary(self, episode: _Episode) -> None:
        if episode.repeated:
            seconds = int(self.window.total_seconds())
            window = f"{seconds // 60}분" if seconds % 60 == 0 else f"{seconds}초"
            message = f"{episode.message} ({window} 동안 {episode.repeated}번)"
            self.emit(
                episode.message.with_text(message)
                if isinstance(episode.message, Notice)
                else message
            )

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [
                {
                    "key": list(key) if isinstance(key, tuple) else [str(key)],
                    "start": e.start.isoformat(),
                    "last": e.last.isoformat(),
                    "notice": e.message.record()
                    if isinstance(e.message, Notice)
                    else {"message": e.message, "code": str(key)},
                    "repeated": e.repeated,
                }
                for key, e in self._episodes.items()
            ]

    def restore(self, records: list[dict[str, Any]]) -> None:
        with self._lock:
            self._episodes = {
                tuple(r["key"]): _Episode(
                    datetime.fromisoformat(r["start"]),
                    datetime.fromisoformat(r["last"]),
                    Notice(**r["notice"]),
                    r["repeated"],
                )
                for r in records
            }
