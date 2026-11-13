"""M3-05 adapter boundary: authenticated Telegram update metadata, same command service.

Transport polling/delivery is assembled by the Trader; tests use synthetic updates.
The confirmation callback retains the original command update_id as its idempotency key.
"""

from typing import Any

from .models import Context
from .service import CommandService


class TelegramCommands:
    def __init__(self, service: CommandService) -> None:
        self.service = service

    async def handle(
        self,
        *,
        user_id: int,
        chat_id: int,
        update_id: int,
        command: str,
        target: str | None = None,
        confirmation_id: str | None = None,
        manually_checked: bool = False,
    ) -> dict[str, Any]:
        context = Context(
            "telegram",
            f"telegram:{user_id}",
            str(chat_id),
            self.service.environment,
            self.service.account_id,
            str(update_id),
        )
        return await self.service.request(
            context,
            command.removeprefix("/"),
            target=target,
            confirmation_id=confirmation_id,
            manually_checked=manually_checked,
        )
