"""Local SQLite path policy, shared by configuration and database startup."""

import ctypes
import os
from pathlib import Path


def local_database_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if str(resolved).startswith(("\\\\", "//")):
        raise ValueError("SQLite requires a local disk")
    if any(
        name in part.casefold()
        for part in resolved.parent.parts
        for name in ("onedrive", "dropbox", "google drive", "icloud")
    ):
        raise ValueError("SQLite must not reside in a synchronized folder")
    if os.name == "nt" and ctypes.windll.kernel32.GetDriveTypeW(resolved.anchor) == 4:
        raise ValueError("SQLite must not reside on a network drive")
    return resolved
