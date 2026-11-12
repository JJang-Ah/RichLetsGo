"""Run the local dashboard: python -m richletsgo_dashboard [--db FILE] [--environment demo].

With --db it reads the Trader's SQLite file read-only; without it, sample data is shown
(with the sample banner) so screens can be checked before a database exists.
"""

import argparse
from pathlib import Path

import uvicorn

from richletsgo.keystore import CredentialError, KeyStore, windows_backend

from .app import create_app, monotonic
from .control import Disconnected
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
    parser.add_argument("--db", type=Path, help="Trader SQLite file (storage.<env>.db_file)")
    args = parser.parse_args()
    require_loopback(args.host, args.port)
    source: DashboardSource = (
        SqliteSource(args.db, args.environment)
        if args.db
        else FakeSource(environment=args.environment)
    )
    app = create_app(
        source,
        Auth(code=access_code, clock=monotonic),
        Disconnected(),
        port=args.port,
    )
    uvicorn.run(app, host=LOOPBACK, port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
