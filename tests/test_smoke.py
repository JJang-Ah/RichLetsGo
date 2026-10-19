"""Verify the supported interpreter and all M0 package entry points."""

import sys
from importlib import import_module
from importlib.metadata import version

import pytest

import richletsgo

PACKAGES = (
    "richletsgo",
    "richletsgo.config",
    "richletsgo.keystore",
    "richletsgo.marketdata",
    "richletsgo.strategy",
    "richletsgo.portfolio",
    "richletsgo.ai",
    "richletsgo.risk",
    "richletsgo.oms",
    "richletsgo.storage",
    "richletsgo.notify",
    "richletsgo.health",
    "richletsgo.exchange",
    "richletsgo.exchange.bybit",
    "richletsgo.exchange.simulated",
    "richletsgo_watchdog",
    "richletsgo_dashboard",
)


@pytest.mark.parametrize("package_name", PACKAGES)
def test_package_imports(package_name: str) -> None:
    assert import_module(package_name).__name__ == package_name


def test_supported_python() -> None:
    assert sys.version_info >= (3, 12)


def test_distribution_version() -> None:
    assert richletsgo.__version__ == version("richletsgo") == "0.0.1"
