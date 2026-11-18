"""NSSM entry gate: bound starts across process restarts, then run the real module.

No subprocess or process termination API. Exit 78 stops NSSM restart policy.
"""

import argparse
import asyncio
import json
import os
import runpy
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

from richletsgo.keystore import KeyStore, windows_backend
from richletsgo.notify.bot import BotClient
from richletsgo.notify.wording import watchdog

from .config import Settings, load

MODULES = {
    "Trader": "richletsgo.app",
    "Watchdog": "richletsgo_watchdog",
    "Dashboard": "richletsgo_dashboard",
}


def admit_start(path: Path, now: float, limit: int, window: int) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous: dict[str, Any] = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"starts": [], "blocked": False}
    )
    if type(previous["blocked"]) is not bool or not isinstance(previous["starts"], list):
        raise ValueError("invalid start ledger")
    starts = [v for v in previous["starts"] if now - v < window]
    blocked = previous["blocked"] or len(starts) >= limit
    if not blocked:
        starts.append(now)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump({"starts": starts, "blocked": blocked}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return not blocked


async def stopped(settings: Settings, role: str) -> None:
    """Best effort bounded send, including when Watchdog itself exhausts its budget."""
    try:
        token = KeyStore(windows_backend()).get(settings.bot_item)
        async with aiohttp.ClientSession(trust_env=False) as session:
            bot = BotClient(session, token, settings.timeout)
            for user in settings.users:
                try:
                    await bot.call(
                        "sendMessage",
                        {
                            "chat_id": user,
                            "text": watchdog(settings.environment, "restart_limit_" + role),
                        },
                    )
                except Exception:
                    print("[Service] stop notification unavailable", flush=True)
    except Exception:
        print("[Service] stop notification unavailable", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=MODULES)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--account", required=True)
    args = parser.parse_args()
    try:
        settings = load(args.config)
        if not admit_start(
            settings.state_dir / (args.service + ".json"),
            time.time(),
            settings.restart_limit,
            settings.restart_window,
        ):
            print(
                "[Service] restart budget exhausted; manual investigation/reset required",
                flush=True,
            )
            asyncio.run(stopped(settings, args.service))
            raise SystemExit(78)
    except Exception:
        print("[Service] start ledger unavailable; refusing automatic restart", flush=True)
        raise SystemExit(78) from None
    sys.argv = [MODULES[args.service], "--config", str(args.config.resolve())]
    if args.service == "Trader":
        sys.argv.extend(["--account", args.account])
    runpy.run_module(MODULES[args.service], run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
