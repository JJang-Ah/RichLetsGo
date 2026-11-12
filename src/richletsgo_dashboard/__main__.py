"""Run the local dashboard: python -m richletsgo_dashboard [--db FILE] [--environment demo].

With --db it reads the Trader's SQLite file read-only; without it, sample data is shown
(with the sample banner) so screens can be checked before a database exists.
"""

import argparse
from pathlib import Path

import uvicorn

from richletsgo.config import load_config
from richletsgo.keystore import CredentialError, KeyStore, windows_backend

from .app import create_app, monotonic
from .control import Disconnected, HttpTraderClient, TraderClient
from .security import LOOPBACK, Auth, require_loopback
from .source import DashboardSource, FakeSource
from .sqlite_source import SqliteSource

ACCESS_ITEM = "richletsgo/dashboard-access"


def access_code() -> str | None:
    try:
        return KeyStore(windows_backend()).get(ACCESS_ITEM).reveal()
    except CredentialError:
        return None  # login disabled until the user stores a code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default=LOOPBACK)
    parser.add_argument("--environment", choices=("demo", "live"), default="demo")
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--db", type=Path, help="Read-only Trader database without control")
    sources.add_argument(
        "--config", type=Path, help="Read-only SQLite source and control configuration"
    )
    args = parser.parse_args()
    require_loopback(args.host, args.port)
    source: DashboardSource = (
        SqliteSource(args.db, args.environment)
        if args.db
        else FakeSource(environment=args.environment)
    )
    trader: TraderClient = Disconnected()
    code = access_code
    if args.config:
        config = load_config(args.config)
        args.port = config.control.dashboard_port
        source = SqliteSource(config.storage.db_file, config.environment.value)
        identity = source.snapshot().overview
        if identity.environment != config.environment.value:
            raise ValueError("dashboard configuration/database environment mismatch")
        keys = KeyStore(windows_backend())

        def configured_code() -> str | None:
            try:
                return keys.get(config.keystore["dashboard_access"]).reveal()
            except CredentialError:
                return None

        code = configured_code
        token_item = config.keystore.get("control_token_" + identity.environment)
        if token_item:
            try:
                trader = HttpTraderClient(
                    port=config.control.trader_port,
                    dashboard_port=args.port,
                    token=keys.get(token_item),
                    environment=identity.environment,
                    account_id=identity.account_id,
                )
            except CredentialError:
                pass
    app = create_app(source, Auth(code=code, clock=monotonic), trader, port=args.port)
    uvicorn.run(app, host=LOOPBACK, port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
