"""Client side of the Trader control API (M5-01b). Until it exists, Trader is disconnected.

The dashboard only forwards commands; actor, environment check, confirmation and
recording all happen in the Trader's CommandService.
"""

from typing import Protocol

COMMANDS = ("pause", "resume", "cancel", "resolve")


class TraderClient(Protocol):
    def connected(self) -> bool: ...


class Disconnected:
    """Placeholder until M5-01b: every control is disabled, screens still render."""

    def connected(self) -> bool:
        return False
