"""Watchdog view on the existing authenticated loopback control application."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI


def install_health(app: FastAPI, snapshot: Callable[[], dict[str, Any]]) -> None:
    @app.get("/watchdog")
    def watchdog() -> dict[str, Any]:
        return snapshot()
