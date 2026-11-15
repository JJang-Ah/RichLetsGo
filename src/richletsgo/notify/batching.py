"""Clock-driven warning episodes, shared by logging and notification delivery."""

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock

from richletsgo.domain import Clock


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

    def _summary(self, episode: _Episode) -> None:
        if episode.repeated:
            self.emit(
                f"{episode.message} (repeated {episode.repeated}; "
                f"{episode.start.isoformat()} .. {episode.last.isoformat()})"
            )
