"""Independent monitor settings: no strategy, credential, or DB access on load."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from richletsgo.config import integer, text
from richletsgo.storage.paths import local_database_path


@dataclass(frozen=True)
class Settings:
    environment: str
    port: int
    token_item: str
    bot_item: str
    users: tuple[int, ...]
    db: Path
    disk: Path
    state_dir: Path
    interval: int
    timeout: int
    grace: int
    stale: int
    repeat: int
    minimum_free: int
    restart_limit: int
    restart_window: int
    restart_delay: int


def load(path: Path) -> Settings:
    try:
        path = path.resolve()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        env = text(data, "environment")
        if env not in {"demo", "live"}:
            raise ValueError
        w = data["watchdog"]

        def location(value: str) -> Path:
            return (path.parent / value).resolve()

        users = data["telegram"]["allowed_user_ids"]
        if not users or any(type(v) is not int or v <= 0 for v in users):
            raise ValueError
        port = integer(data["control"], "port")
        if port > 65535:
            raise ValueError
        return Settings(
            env,
            port,
            text(data["keystore"], "control_token_" + env),
            text(data["keystore"], "telegram_bot"),
            tuple(set(users)),
            local_database_path(location(text(data["storage"][env], "db_file"))),
            location(text(w, "disk_path")),
            location(text(w, "state_dir")),
            *(
                integer(w, k)
                for k in (
                    "interval_seconds",
                    "timeout_seconds",
                    "unhealthy_seconds",
                    "boundary_stale_seconds",
                    "repeat_seconds",
                    "minimum_free_bytes",
                    "restart_limit",
                    "restart_window_seconds",
                    "restart_delay_seconds",
                )
            ),
        )
    except Exception:
        raise ValueError("invalid watchdog configuration") from None
