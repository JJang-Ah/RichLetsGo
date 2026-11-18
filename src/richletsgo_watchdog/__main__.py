"""Independent, send-only Watchdog entry point."""

import argparse
import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from richletsgo.keystore import KeyStore, windows_backend
from richletsgo.notify.bot import BotClient
from richletsgo.notify.wording import stamp

from .config import load
from .monitor import Monitor, faults, health, local_faults


async def run(path: Path) -> None:
    settings = load(path)
    keys = KeyStore(windows_backend())
    token, bot_token = keys.get(settings.token_item), keys.get(settings.bot_item)
    async with aiohttp.ClientSession(trust_env=False) as session:
        monitor = Monitor(settings, BotClient(session, bot_token, settings.timeout))
        while True:
            try:
                value = await health(session, settings, token)
            except OSError:
                value = None
            active = faults(value, datetime.now(UTC), settings.stale)
            details = {}
            if value and "boundary_stale" in active:
                details["boundary_stale"] = "마지막 " + stamp(value.get("last_boundary")) + " KST"
            active.update(await local_faults(settings, details))
            await monitor.update(active, time.monotonic(), details)
            await asyncio.sleep(settings.interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.config))
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit(
            "[Watchdog] startup/runtime failure; check configuration and credentials"
        ) from None


if __name__ == "__main__":
    main()
