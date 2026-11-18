"""Offline M3-06 tests: synthetic health, temporary DB, fake Bot only."""

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from richletsgo.keystore import Secret
from richletsgo.notify.bot import BotError
from richletsgo_watchdog.config import load
from richletsgo_watchdog.monitor import Monitor, database_ok, faults, health, local_faults
from richletsgo_watchdog.service import admit_start


@pytest.fixture
def settings(tmp_path):
    source = (
        Path("config.example.toml")
        .read_text(encoding="utf-8")
        .replace('environment = "backtest"', 'environment = "demo"', 1)
    )
    config = tmp_path / "config.toml"
    config.write_text(source, encoding="utf-8")
    return load(config)


class Bot:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def call(self, method, payload):
        assert method == "sendMessage"
        if self.fail:
            raise BotError(429, 100)
        self.calls.append(payload)


@pytest.mark.parametrize(
    "cause",
    [
        "health_unavailable",
        "ready",
        "private_ws",
        "boundary_stale",
        "database",
        "disk_low",
        "restart_limit_Trader",
    ],
)
def test_grace_repeat_recovery(settings, cause):
    async def run():
        bot = Bot()
        monitor = Monitor(settings, bot)
        await monitor.update({cause}, 0)
        await monitor.update(set(), settings.grace - 1)
        assert not bot.calls
        await monitor.update({cause}, 200)
        await monitor.update({cause}, 200 + settings.grace)
        assert len(bot.calls) == 1
        await monitor.update({cause}, 201 + settings.grace)
        assert len(bot.calls) == 1
        await monitor.update({cause}, 200 + settings.grace + settings.repeat)
        assert len(bot.calls) == 2
        await monitor.update(set(), 201 + settings.grace + settings.repeat)
        await monitor.update(set(), 202 + settings.grace + settings.repeat)
        assert len(bot.calls) == 3 and bot.calls[-1]["text"].startswith("🟢")
        assert all("[데모 감시]" in v["text"] for v in bot.calls)

    asyncio.run(run())


def test_bot_failure_keeps_pending_and_honors_retry(settings):
    async def run():
        bot = Bot()
        monitor = Monitor(settings, bot)
        await monitor.update({"ready"}, 0)
        bot.fail = True
        await monitor.update({"ready"}, settings.grace)
        bot.fail = False
        await monitor.update({"ready"}, settings.grace + 99)
        assert not bot.calls
        await monitor.update({"ready"}, settings.grace + 100)
        assert len(bot.calls) == 1

    asyncio.run(run())


def test_faults_no_health_body_in_alerts():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    good = dict(alive=True, ready=True, private_ws=True, last_boundary=now.isoformat())
    assert not faults(good, now, 3600)
    assert faults(None, now, 3600) == {"health_unavailable"}
    for value in (
        None,
        "bad-secret",
        (now - timedelta(hours=2)).isoformat(),
        (now + timedelta(hours=2)).isoformat(),
    ):
        assert "boundary_stale" in faults({**good, "last_boundary": value}, now, 3600)
    assert faults({**good, "ready": False, "private_ws": False}, now, 3600) == {
        "ready",
        "private_ws",
    }


def test_db_missing_corrupt_environment_readonly_and_disk(settings, tmp_path, monkeypatch):
    assert not database_ok(settings.db, 1, "demo")
    assert not settings.db.exists()
    settings.db.parent.mkdir(parents=True)
    settings.db.write_bytes(b"corrupt")
    assert not database_ok(settings.db, 1, "demo")
    settings.db.unlink()
    with sqlite3.connect(settings.db) as connection:
        connection.execute("CREATE TABLE meta(key TEXT, value TEXT)")
        connection.execute("INSERT INTO meta VALUES ('environment','demo')")
    connection.close()
    before = settings.db.read_bytes()
    assert database_ok(settings.db, 1, "demo")
    assert not database_ok(settings.db, 1, "live")
    assert settings.db.read_bytes() == before
    monkeypatch.setattr(
        "richletsgo_watchdog.monitor.shutil.disk_usage", lambda _: SimpleNamespace(free=0)
    )
    assert asyncio.run(local_faults(settings)) == {"disk_low"}
    settings.db.unlink()

    async def run():
        bot = Bot()
        monitor = Monitor(settings, bot)
        active = await local_faults(settings)
        await monitor.update(active, 0)
        await monitor.update(active, settings.grace)
        assert any("database" in v["text"] for v in bot.calls)

    asyncio.run(run())


class Response:
    status = 200

    def __init__(self, raw):
        self.raw = raw
        self.content = self

    async def read(self, size):
        return self.raw[:size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class Session:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.raw)


def test_health_token_only_in_loopback_header_no_redirect(settings):
    session = Session(b'{"ready": true}')
    assert asyncio.run(health(session, settings, Secret("sentinel-secret"))) == {"ready": True}
    url, args = session.calls[0]
    assert url == f"http://127.0.0.1:{settings.port}/watchdog"
    assert args["headers"] == {"Authorization": "Bearer sentinel-secret"}
    assert args["allow_redirects"] is False
    for raw in (b"sentinel-secret", b"x" * 65537, b"[]"):
        with pytest.raises(OSError) as error:
            asyncio.run(health(Session(raw), settings, Secret("sentinel-secret")))
        assert "sentinel-secret" not in str(error.value)
        assert error.value.__suppress_context__


def test_restart_budget_durable_latched_and_window(settings):
    path = settings.state_dir / "Trader.json"
    for now in range(settings.restart_limit):
        assert admit_start(path, now, settings.restart_limit, settings.restart_window)
    assert not admit_start(path, 6, settings.restart_limit, settings.restart_window)
    assert not admit_start(path, 99999, settings.restart_limit, settings.restart_window)
    assert "restart_limit_Trader" in asyncio.run(local_faults(settings))
    other = settings.state_dir / "Dashboard.json"
    assert admit_start(other, 0, 1, 10)
    assert admit_start(other, 11, 1, 10)


def test_service_wrapper_budget_blocks_execution(tmp_path, settings, monkeypatch):
    import richletsgo_watchdog.service as service

    monkeypatch.setattr(service, "load", lambda _: replace(settings, restart_limit=1))
    monkeypatch.setattr(service.time, "time", lambda: 1)
    calls = []
    notices = []

    async def stopped(*args):
        notices.append(args)

    monkeypatch.setattr(service, "stopped", stopped)
    monkeypatch.setattr(service.runpy, "run_module", lambda *a, **kw: calls.append(a))
    args = ["service", "Trader", "--config", str(tmp_path / "fake.toml"), "--account", "synthetic"]
    monkeypatch.setattr(service.sys, "argv", args.copy())
    service.main()
    assert calls == [("richletsgo.app",)]
    monkeypatch.setattr(service.sys, "argv", args.copy())
    with pytest.raises(SystemExit) as error:
        service.main()
    assert error.value.code == 78 and len(calls) == 1 and len(notices) == 1


def test_config_no_db_keys_or_strategy_access(settings, tmp_path):
    assert settings.db.is_absolute() and settings.state_dir.is_absolute()
    assert not settings.db.exists()
    bad = tmp_path / "bad.toml"
    bad.write_text('secret = "sentinel-secret"')
    with pytest.raises(ValueError, match="invalid watchdog configuration") as error:
        load(bad)
    assert "sentinel-secret" not in str(error.value)


def test_corrupt_start_ledger_fails_closed_and_monitor_reports(settings):
    path = settings.state_dir / "Trader.json"
    path.parent.mkdir(parents=True)
    for raw in ("broken", "[]", '{"starts": [], "blocked": "bad"}'):
        path.write_text(raw)
        with pytest.raises((ValueError, TypeError)):
            admit_start(path, 0, 5, 60)
    path.write_text("[]")
    assert "restart_state_Trader" in asyncio.run(local_faults(settings))


def test_http_denial_and_exception_are_sanitized(settings):
    class Denied(Session):
        def get(self, url, **kwargs):
            response = Response(b'{"token":"secret-marker"}')
            response.status = 403
            return response

    class Broken(Session):
        def get(self, url, **kwargs):
            raise RuntimeError("secret-marker")

    for session in (Denied(b""), Broken(b"")):
        with pytest.raises(OSError) as error:
            asyncio.run(health(session, settings, Secret("secret-marker")))
        assert str(error.value) == "Trader health unavailable"
        assert error.value.__suppress_context__


def test_connection_faults_share_delivery_without_merging_recovery(settings):
    async def run():
        bot = Bot()
        monitor = Monitor(settings, bot)
        await monitor.update({"ready", "private_ws"}, 0)
        await monitor.update({"ready", "private_ws"}, settings.grace)
        assert len(bot.calls) == 1
        assert all(code in bot.calls[0]["text"] for code in ("코드 ready", "코드 private_ws"))
        await monitor.update({"ready"}, settings.grace + 1)
        assert len(bot.calls) == 2 and "연결 복구" in bot.calls[-1]["text"]
        assert "코드 ready" not in bot.calls[-1]["text"]

    asyncio.run(run())
