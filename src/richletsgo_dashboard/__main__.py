"""Run the local dashboard: python -m richletsgo_dashboard [--port 8765] [--environment demo].

M5-01a serves the fake data source; the SQLite reader replaces it after M3-02.
"""

import argparse

import uvicorn

from richletsgo.keystore import CredentialError, KeyStore, windows_backend

from .app import create_app, monotonic
from .control import Disconnected
from .security import LOOPBACK, Auth, require_loopback
from .source import FakeSource

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
    args = parser.parse_args()
    require_loopback(args.host, args.port)
    app = create_app(
        FakeSource(environment=args.environment),
        Auth(code=access_code, clock=monotonic),
        Disconnected(),
        port=args.port,
    )
    uvicorn.run(app, host=LOOPBACK, port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
