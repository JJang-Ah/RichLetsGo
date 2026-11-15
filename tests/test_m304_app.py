"""Entrypoint, health and scheduler contracts without opening a socket or key store."""

import asyncio
import tomllib

import pytest
from fastapi.testclient import TestClient
from test_control import setup
from test_service_config import config_file

from richletsgo.app import TraderServer, loop_policy
from richletsgo.control.api import create_control_app
from richletsgo.health import install_health
from richletsgo.keystore import Secret


def test_loop_policy_requires_every_setting_and_rejects_implicit_values(tmp_path):
    path = config_file(tmp_path)
    policy = loop_policy(path)
    assert policy.tick.total_seconds() == 1
    assert policy.bar_wait.total_seconds() == 90
    text = path.read_text(encoding="utf-8")
    for field in tomllib.loads(text)["trader"]:
        path.write_text(text.replace(field + " = ", "extra_" + field + " = "), encoding="utf-8")
        with pytest.raises(ValueError):
            loop_policy(path)


def test_watchdog_uses_existing_loopback_and_token_guards(tmp_path):
    async def prepare():
        return (await setup(tmp_path, "memory"))[1]

    service = asyncio.run(prepare())
    app = create_control_app(
        service,
        operator_id="operator",
        token=Secret("synthetic-control-token"),
        port=9812,
        dashboard_port=9813,
    )
    snapshot = {
        "alive": True,
        "ready": False,
        "last_boundary": None,
        "private_ws": False,
        "last_commit": None,
        "pauses": [],
    }
    install_health(app, lambda: snapshot)
    with TestClient(app, base_url="http://127.0.0.1:9812") as client:
        assert client.get("/watchdog").status_code == 401
        headers = {"authorization": "Bearer synthetic-control-token"}
        response = client.get("/watchdog", headers=headers)
        assert response.status_code == 200 and response.json() == snapshot
        assert "token" not in response.text
        assert (
            client.get("/watchdog", headers={**headers, "host": "attacker.example"}).status_code
            == 400
        )
    with pytest.raises(ValueError):
        create_control_app(
            service,
            operator_id="operator",
            token=Secret("token"),
            port=9812,
            dashboard_port=9813,
            host="0.0.0.0",
        )


def test_trader_owns_signal_handlers():
    import signal
    from types import SimpleNamespace

    server = TraderServer(SimpleNamespace())
    before = signal.getsignal(signal.SIGINT)
    with server.capture_signals():
        assert signal.getsignal(signal.SIGINT) == before
