"""Read-only probes and bounded per-cause/per-recipient notification episodes."""

import asyncio
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from richletsgo.keystore import Secret
from richletsgo.notify.bot import Bot, BotError
from richletsgo.notify.wording import watchdog

from .config import Settings


async def health(
    session: aiohttp.ClientSession, settings: Settings, token: Secret
) -> dict[str, Any]:
    try:
        async with session.get(
            f"http://127.0.0.1:{settings.port}/watchdog",
            headers={"Authorization": "Bearer " + token.reveal()},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=settings.timeout),
        ) as response:
            if response.status != 200:
                raise ValueError
            raw = await response.content.read(65537)
            if len(raw) > 65536:
                raise ValueError
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
    except Exception:
        raise OSError("Trader health unavailable") from None


def database_ok(path: Path, timeout: int, environment: str) -> bool:
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=timeout)
        try:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute("SELECT value FROM meta WHERE key='environment'").fetchone()
            return bool(row == (environment,))
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False


def faults(value: dict[str, Any] | None, now: datetime, stale: int) -> set[str]:
    result: set[str] = set()
    if value is None:
        return {"health_unavailable"}
    for field in ("alive", "ready", "private_ws"):
        if value.get(field) is not True:
            result.add(field)
    try:
        boundary = datetime.fromisoformat(value["last_boundary"])
        age = (now - boundary).total_seconds()
        if age < 0 or age > stale:
            result.add("boundary_stale")
    except (ValueError, TypeError, KeyError):
        result.add("boundary_stale")
    return result


@dataclass
class Episode:
    since: float
    sent: float | None = None


class Monitor:
    def __init__(self, settings: Settings, bot: Bot) -> None:
        self.settings, self.bot = settings, bot
        self.retry_at = 0.0
        self.episodes: dict[tuple[str, int], Episode] = {}

    async def update(
        self, active: set[str], now: float, details: dict[str, str] | None = None
    ) -> None:
        for cause in active:
            for user in self.settings.users:
                self.episodes.setdefault((cause, user), Episode(now))
        if now < self.retry_at:
            return
        due: dict[tuple[int, str, bool], list[tuple[str, int]]] = {}
        for key, episode in list(self.episodes.items()):
            cause, user = key
            recovered = cause not in active
            if recovered and episode.sent is None:
                del self.episodes[key]
                continue
            if not recovered and (
                now - episode.since < self.settings.grace
                or (episode.sent is not None and now - episode.sent < self.settings.repeat)
            ):
                continue
            group = "connection" if cause in {"ready", "private_ws"} else cause
            due.setdefault((user, group, recovered), []).append(key)
        for (user, _, recovered), keys in due.items():
            message = "\n".join(
                watchdog(
                    self.settings.environment,
                    k[0],
                    recovered,
                    int((now - self.episodes[k].since) // 60),
                    self.episodes[k].sent is not None,
                )
                + (" · " + details[k[0]] if details and k[0] in details and not recovered else "")
                for k in sorted(keys)
            )
            try:
                await self.bot.call("sendMessage", {"chat_id": user, "text": message})
            except BotError as error:
                self.retry_at = now + max(self.settings.interval, error.retry_after)
                print("[Watchdog] notification unavailable", flush=True)
                return
            except Exception:
                # Never include remote exception text, token URLs or health bodies.
                print("[Watchdog] notification unavailable", flush=True)
                continue
            for key in keys:
                if recovered:
                    del self.episodes[key]
                else:
                    self.episodes[key].sent = now


async def local_faults(settings: Settings, details: dict[str, str] | None = None) -> set[str]:
    result: set[str] = set()
    if not await asyncio.to_thread(
        database_ok, settings.db, settings.timeout, settings.environment
    ):
        result.add("database")
    try:
        free = shutil.disk_usage(settings.disk).free
        if free < settings.minimum_free:
            if details is not None:
                details["disk_low"] = f"{free / 10**9:.1f} GB 남음"
            result.add("disk_low")
    except OSError:
        result.add("disk_unavailable")
    for name in ("Trader", "Dashboard", "Watchdog"):
        path = settings.state_dir / (name + ".json")
        try:
            if path.exists() and json.loads(path.read_text(encoding="utf-8"))["blocked"]:
                result.add("restart_limit_" + name)
        except (OSError, ValueError, KeyError, TypeError):
            result.add("restart_state_" + name)
    return result
