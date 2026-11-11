"""Explicit local migration and online backup commands (no exchange connection)."""

import argparse
import asyncio
from pathlib import Path

from richletsgo.config import load_config

from .sqlite import SqliteDatabase


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate", "backup"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--account", required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    database = SqliteDatabase(
        config.storage.db_file,
        args.account,
        config.environment,
        working_set_days=config.storage.working_set_days,
        busy_timeout_seconds=config.storage.busy_timeout_seconds,
        outcome_timeout_seconds=config.storage.outcome_timeout_seconds,
    )

    async def run() -> None:
        if args.command == "migrate":
            await database.migrate()
        else:
            print(await database.backup(config.paths.backup_dir))

    asyncio.run(run())


if __name__ == "__main__":
    main()
