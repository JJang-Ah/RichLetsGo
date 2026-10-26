"""Windows Credential Manager access. Importing this module never reads credentials."""

import importlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from richletsgo.domain.clock import require_utc


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class Secret:
    _value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self._value, str) or not self._value:
            raise CredentialError("empty credential")

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return "***"


@dataclass(frozen=True)
class ApiCredential:
    key: Secret
    secret: Secret
    created_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.created_at)


class Backend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...


def windows_backend() -> Backend:
    if sys.platform != "win32":
        raise CredentialError("Windows Credential Manager is required")
    try:
        # Explicit backend: do not honor plaintext/fallback plugins or environment overrides.
        backend: Backend = importlib.import_module("keyring.backends.Windows").WinVaultKeyring()
        return backend
    except Exception:
        raise CredentialError("Windows Credential Manager is unavailable") from None


class KeyStore:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    def get(self, item: str) -> Secret:
        try:
            value = self.backend.get_password(item, "credential")
        except Exception:
            raise CredentialError("credential backend read failed") from None
        if not value:
            raise CredentialError(f"missing credential item: {item}")
        return Secret(value)

    def set(self, item: str, value: Secret) -> None:
        try:
            self.backend.set_password(item, "credential", value.reveal())
        except Exception:
            raise CredentialError("credential backend write failed") from None

    def api(self, item: str, created_at: datetime) -> ApiCredential:
        value = self.get(item)
        try:
            row = json.loads(value.reveal())
            if set(row) != {"key", "secret"}:
                raise ValueError
            return ApiCredential(Secret(row["key"]), Secret(row["secret"]), created_at)
        except (ValueError, TypeError, KeyError):
            raise CredentialError(f"invalid API credential item: {item}") from None
