"""Structured cause identities; string compatibility is only for existing log sinks."""

from typing import Any, Self


class Notice(str):
    code: str
    symbol: str
    strategy: str
    related: str
    resolved: bool

    def __new__(
        cls,
        message: str,
        *,
        code: str,
        symbol: str = "",
        strategy: str = "",
        related: str = "",
        resolved: bool = False,
    ) -> Self:
        obj = super().__new__(cls, message)
        obj.code, obj.symbol, obj.strategy, obj.related, obj.resolved = (
            code,
            symbol,
            strategy,
            related,
            resolved,
        )
        return obj

    @property
    def key(self) -> tuple[str, ...]:
        return self.code, self.symbol, self.strategy, self.related

    def record(self) -> dict[str, Any]:
        return dict(
            message=str(self),
            code=self.code,
            symbol=self.symbol,
            strategy=self.strategy,
            related=self.related,
            resolved=self.resolved,
        )

    def with_text(self, message: str) -> "Notice":
        return Notice(**{**self.record(), "message": message})
