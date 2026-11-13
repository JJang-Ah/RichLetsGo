"""D106 command contracts on synthetic memory/SQLite and local HTTP test transports."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from test_oms import order
from test_risk import NOW, limits

from richletsgo.control.api import create_control_app
from richletsgo.control.models import CommandPolicy, Context
from richletsgo.control.service import CommandDenied, CommandService
from richletsgo.control.telegram import TelegramCommands
from richletsgo.domain import Environment, ManualClock, OrderState
from richletsgo.keystore import Secret
from richletsgo.risk.equity import AccountSnapshot, EquityTracker
from richletsgo.risk.pause import Pause, PauseKind, PauseState
from richletsgo.storage.memory import InMemoryDatabase
from richletsgo.storage.sqlite import SqliteDatabase, open_readonly


async def setup(tmp_path, backend="sqlite", source="web"):
    clock = ManualClock(NOW)
    db = (
        SqliteDatabase(tmp_path / "control.sqlite", "acct", Environment.DEMO, now=clock.now)
        if backend == "sqlite"
        else InMemoryDatabase("acct", Environment.DEMO)
    )
    if backend == "sqlite":
        await db.migrate()
    notices = []

    async def notify(message):
        notices.append(json.loads(message))

    async def resolver(txn):
        return await db.resolve(txn)

    service = CommandService(
        account_id="acct",
        environment=Environment.DEMO,
        uow_factory=db.unit_of_work,
        pauses=PauseState(),
        tracker=EquityTracker.open(AccountSnapshot(NOW, D(1000), D(0))),
        limits=limits(),
        allowed_ids={"web:operator", "telegram:1"},
        clock=clock,
        policy=CommandPolicy(timedelta(seconds=5), 2),
        account_lock=asyncio.Lock(),
        notify=notify,
        resolve_commit=resolver,
    )
    context = Context(
        source,
        "web:operator" if source == "web" else "telegram:1",
        "a" * 64,
        Environment.DEMO,
        "acct",
        "request-1",
    )
    return db, service, context, clock, notices


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("source", ["web", "telegram"])
def test_pause_idempotency_resume_tolerance_and_binding(tmp_path, backend, source):
    async def run():
        db, service, ctx, clock, notices = await setup(tmp_path, backend, source)
        first = await service.request(ctx, "pause")
        assert service.pauses.has(PauseKind.ENTRY_PAUSED_MANUAL)
        assert await service.request(ctx, "pause") == first
        assert len(notices) == 1
        service.pauses.add(Pause(PauseKind.HALTED, "drawdown", clock.now()))
        service.tracker.observe(AccountSnapshot(clock.now(), D(1000), D(-100)))
        ctx = replace(ctx, request_id="resume-1")
        prompt = await service.request(ctx, "resume")
        confirmation = prompt["confirmation_id"]
        for changed in (
            replace(ctx, session_id="other"),
            replace(ctx, actor_id=source + ":other"),
            replace(ctx, environment=Environment.LIVE),
        ):
            with pytest.raises(CommandDenied):
                await service.request(changed, "resume", confirmation_id=confirmation)
        service.tracker.observe(AccountSnapshot(clock.now(), D(1000), D("-100.5")))
        results = await asyncio.gather(
            *[service.request(ctx, "resume", confirmation_id=confirmation) for _ in range(2)]
        )
        assert results[0] == results[1] and results[0]["state"] == "completed"
        assert results[0]["resume_record"]["peak_reset"]["new_risk_peak"] == "899.5"
        assert service.tracker.risk_peak == D("899.5")
        assert not service.pauses.has(PauseKind.HALTED)
        assert len(notices) == 2
        service.prompts.clear()  # restart loses confirmations, but request replay still returns result
        assert await service.request(ctx, "resume", confirmation_id=confirmation) == results[0]
        if backend == "sqlite":
            connection = open_readonly(db.path)
            try:
                assert connection.execute(
                    "SELECT source,state FROM commands WHERE request_id='resume-1'"
                ).fetchone() == (source, "completed")
            finally:
                connection.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["revision", "equity", "expiry", "revoked"])
def test_resume_requires_fresh_confirmation(tmp_path, change):
    async def run():
        _, service, ctx, clock, _ = await setup(tmp_path)
        service.pauses.add(Pause(PauseKind.HALTED, "drawdown", clock.now()))
        service.tracker.observe(AccountSnapshot(clock.now(), D(900), D(0)))
        prompt = await service.request(ctx, "resume")
        if change == "revision":
            service.pauses.clear(PauseKind.HALTED)
            service.pauses.add(Pause(PauseKind.HALTED, "drawdown", clock.now()))
        elif change == "equity":
            service.tracker.observe(AccountSnapshot(clock.now(), D(800), D(0)))
        elif change == "expiry":
            clock.advance(timedelta(seconds=60))
        else:
            await service.revoke(ctx.source, ctx.session_id)
            with pytest.raises(CommandDenied):
                await service.request(ctx, "resume", confirmation_id=prompt["confirmation_id"])
            return
        result = await service.request(ctx, "resume", confirmation_id=prompt["confirmation_id"])
        assert result["state"] == "rejected" and service.pauses.has(PauseKind.HALTED)

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("source", ["web", "telegram"])
def test_cancel_fixed_targets_acknowledgement_retry_limit_and_fill_race(tmp_path, backend, source):
    async def run():
        db, service, ctx, clock, notices = await setup(tmp_path, backend, source)
        original, later = order(state=OrderState.OPEN), order(state=OrderState.OPEN, suffix="later")
        async with db.unit_of_work() as uow:
            uow.orders.put(original)
            await uow.commit()
        prompt = await service.request(ctx, "cancel")
        async with db.unit_of_work() as uow:
            uow.orders.put(later)
            await uow.commit()
        first = await service.request(ctx, "cancel", confirmation_id=prompt["confirmation_id"])
        assert first["state"] == "accepted" and len(first["targets"]) == 1
        async with db.unit_of_work() as uow:
            actions = uow.portfolio.actions()
            assert len(actions) == 1 and actions[0].order_key == original.key
            uow.portfolio.acknowledge(
                actions[0].key
            )  # could be TIMEOUT or Accepted: neither proves cancel
            await uow.commit()
        await service.tick()
        assert (await service.request(ctx, "cancel"))["state"] == "accepted"
        clock.advance(timedelta(seconds=5))
        await service.tick()
        async with db.unit_of_work() as uow:
            retry = uow.portfolio.actions()
            assert len(retry) == 1 and retry[0].key != actions[0].key
            assert retry[0].order_key == original.key
            uow.portfolio.acknowledge(retry[0].key)
            await uow.commit()
        clock.advance(timedelta(seconds=5))
        await service.tick()
        result = await service.request(ctx, "cancel")
        assert result["state"] == "completed" and result["targets"][0]["state"] == "failed"
        assert "취소 확인 실패" == result["targets"][0]["reason"]
        assert len(notices) == 2
        # A different command sees both orders; a fill race is recorded as FILLED, not cancel success.
        ctx = replace(ctx, request_id="second")
        prompt = await service.request(ctx, "cancel")
        await service.request(ctx, "cancel", confirmation_id=prompt["confirmation_id"])
        async with db.unit_of_work() as uow:
            uow.orders.put(replace(original, state=OrderState.FILLED))
            uow.orders.put(replace(later, state=OrderState.CANCELLED))
            await uow.commit()
        await service.tick()
        result = await service.request(ctx, "cancel")
        assert result["state"] == "completed"
        assert result["targets"][0]["reason"] == "취소 전에 체결됨"

    asyncio.run(run())


def test_resolve_requires_manual_check_and_unchanged_order(tmp_path):
    async def run():
        db, service, ctx, _, _ = await setup(tmp_path)
        unknown = order(state=OrderState.UNKNOWN)
        async with db.unit_of_work() as uow:
            uow.orders.put(unknown)
            await uow.commit()
        prompt = await service.request(ctx, "resolve", target=unknown.order_link_id)
        with pytest.raises(CommandDenied):
            await service.request(ctx, "cancel", confirmation_id=prompt["confirmation_id"])
        result = await service.request(
            ctx, "resolve", target=unknown.order_link_id, confirmation_id=prompt["confirmation_id"]
        )
        assert result["state"] == "rejected"
        ctx = replace(ctx, request_id="resolve-checked")
        prompt = await service.request(ctx, "resolve", target=unknown.order_link_id)
        result = await service.request(
            ctx,
            "resolve",
            target=unknown.order_link_id,
            confirmation_id=prompt["confirmation_id"],
            manually_checked=True,
        )
        assert result["state"] == "completed"
        async with db.unit_of_work() as uow:
            assert uow.orders.get(unknown.key).state is OrderState.NOT_FOUND

    asyncio.run(run())


@pytest.mark.parametrize("source", ["web", "telegram"])
@pytest.mark.parametrize("committed", [False, True])
def test_commit_unknown_never_reexecutes_and_publishes_only_known_commit(
    tmp_path, source, committed
):
    async def run():
        db, service, ctx, _, _ = await setup(tmp_path, source=source)
        fired = False

        def fault(stage, connection):
            nonlocal fired
            if not fired and stage == ("after_commit" if committed else "commit"):
                fired = True
                raise OSError("synthetic uncertain commit")

        db.fault = fault
        result = await service.request(ctx, "pause")
        assert result["state"] == "confirming"
        assert not service.pauses.has(PauseKind.ENTRY_PAUSED_MANUAL)
        db.fault = None
        result = await service.request(ctx, "pause")
        assert result["state"] == ("completed" if committed else "rejected")
        assert service.pauses.has(PauseKind.ENTRY_PAUSED_MANUAL) is committed
        assert await service.request(ctx, "pause") == result

    asyncio.run(run())


def test_restart_keeps_cancel_and_interrupts_invalid_resume_without_execution(tmp_path):
    async def run():
        db, service, ctx, _, _ = await setup(tmp_path)
        entry = order(state=OrderState.OPEN)
        async with db.unit_of_work() as uow:
            uow.orders.put(entry)
            await uow.commit()
        prompt = await service.request(ctx, "cancel")
        await service.request(ctx, "cancel", confirmation_id=prompt["confirmation_id"])
        async with db.unit_of_work() as uow:
            bad = {
                **ctx.identity(),
                "request_id": "old-resume",
                "command": "resume",
                "target": None,
                "state": "accepted",
                "message": "bad old version",
                "at": NOW.isoformat(),
            }
            uow.recovery.put("command:old-resume", json.dumps(bad))
            await uow.commit()
        # New service instance; same durable DB, all unused confirmation numbers gone.
        _, fresh, _, _, _ = await setup(tmp_path)
        fresh.pauses.add(Pause(PauseKind.HALTED, "test", NOW))
        await fresh.startup()
        async with db.unit_of_work() as uow:
            assert len(uow.portfolio.actions()) == 1
            assert json.loads(uow.recovery.get("command:old-resume"))["state"] == "interrupted"
            uow.orders.put(replace(entry, state=OrderState.CANCELLED))
            await uow.commit()
        assert fresh.pauses.has(PauseKind.HALTED)
        await fresh.tick()
        assert (await fresh.request(ctx, "cancel"))["state"] == "completed"

    asyncio.run(run())


def test_api_token_server_actor_origin_and_forbidden_routes(tmp_path):
    db, service, _, _, notices = asyncio.run(setup(tmp_path))
    app = create_control_app(
        service, operator_id="operator", token=Secret("demo-secret"), port=8766, dashboard_port=8765
    )
    headers = {"Authorization": "Bearer demo-secret", "Origin": "http://127.0.0.1:8765"}
    body = {
        "session_id": "a" * 64,
        "environment": "demo",
        "account_id": "acct",
        "request_id": "web-api",
        "actor_id": "web:forged",
    }
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        assert client.post("/commands/pause", json=body).status_code == 401
        assert (
            client.post(
                "/commands/pause",
                headers={**headers, "Authorization": "Bearer live-secret"},
                json=body,
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/commands/pause", headers={**headers, "Origin": "http://evil.test"}, json=body
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/commands/pause", headers=headers, json={**body, "environment": "live"}
            ).status_code
            == 403
        )
        result = client.post("/commands/pause", headers=headers, json=body)
        assert result.status_code == 200 and result.json()["actor_id"] == "web:operator"
        assert "demo-secret" not in result.text
        for forbidden in ("order", "liquidate", "config", "start", "stop"):
            assert (
                client.post("/commands/" + forbidden, headers=headers, json=body).status_code == 404
            )
        assert len(notices) == 1
    with pytest.raises(ValueError):
        create_control_app(
            service,
            operator_id="operator",
            token=Secret("demo"),
            port=8766,
            dashboard_port=8765,
            host="0.0.0.0",
        )


def test_telegram_adapter_uses_shared_authorization(tmp_path):
    async def run():
        _, service, _, _, notices = await setup(tmp_path)
        adapter = TelegramCommands(service)
        with pytest.raises(CommandDenied):
            await adapter.handle(user_id=2, chat_id=1, update_id=1, command="/pause")
        result = await adapter.handle(user_id=1, chat_id=1, update_id=1, command="/pause")
        assert result["source"] == "telegram" and notices[0]["actor_id"] == "telegram:1"

    asyncio.run(run())


def test_cancel_permanent_rejection_is_failed_not_ack_success(tmp_path):
    from richletsgo.oms.journal import JournalRecord

    async def run():
        db, service, ctx, clock, notices = await setup(tmp_path)
        entry = order(state=OrderState.OPEN)
        async with db.unit_of_work() as uow:
            uow.orders.put(entry)
            await uow.commit()
        prompt = await service.request(ctx, "cancel")
        accepted = await service.request(ctx, "cancel", confirmation_id=prompt["confirmation_id"])
        key = accepted["targets"][0]["action_key"]
        async with db.unit_of_work() as uow:
            uow.portfolio.acknowledge(key)
            journal = JournalRecord(
                "cancel-op",
                "result",
                1,
                "cancel",
                entry.order_link_id,
                "{}",
                '{"code":"REJECTED"}',
                clock.now().isoformat(),
                key,
            )
            uow.recovery.put("action_result:" + key, journal.encode())
            await uow.commit()
        await service.tick()
        result = await service.request(ctx, "cancel")
        assert result["targets"][0]["state"] == "failed"
        assert result["targets"][0]["reason"] == "거래소 영구 거절"
        assert notices[-1]["state"] == "completed"

    asyncio.run(run())


def test_precommit_failure_leaves_pause_and_command_unchanged(tmp_path):
    async def run():
        db, service, ctx, _, _ = await setup(tmp_path)

        def fault(stage, connection):
            if stage == "before_commit":
                raise OSError("synthetic precommit")

        db.fault = fault
        with pytest.raises(OSError):
            await service.request(ctx, "pause")
        assert not service.pauses.active()
        db.fault = None
        async with db.unit_of_work() as uow:
            assert uow.recovery.get("command:" + ctx.request_id) is None
            assert uow.recovery.get("pauses") is None

    asyncio.run(run())


def test_resolve_changed_version_and_cross_source_confirmation_refused(tmp_path):
    async def run():
        db, service, ctx, _, _ = await setup(tmp_path)
        entry = order(state=OrderState.UNKNOWN)
        async with db.unit_of_work() as uow:
            uow.orders.put(entry)
            await uow.commit()
        prompt = await service.request(ctx, "resolve", target=entry.order_link_id)
        other = replace(ctx, source="telegram", actor_id="telegram:1")
        with pytest.raises(CommandDenied):
            await service.request(
                other,
                "resolve",
                target=entry.order_link_id,
                confirmation_id=prompt["confirmation_id"],
                manually_checked=True,
            )
        async with db.unit_of_work() as uow:
            uow.orders.put(replace(entry, state=OrderState.OPEN))
            await uow.commit()
        result = await service.request(
            ctx,
            "resolve",
            target=entry.order_link_id,
            confirmation_id=prompt["confirmation_id"],
            manually_checked=True,
        )
        assert result["state"] == "rejected"

    asyncio.run(run())


def test_dashboard_readonly_forwarding_confirmation_escaping_and_revoke(tmp_path):
    from test_dashboard import BASE, CODE, ORIGIN, csrf, login

    from richletsgo_dashboard.app import COOKIE, create_app
    from richletsgo_dashboard.security import Auth
    from richletsgo_dashboard.sqlite_source import SqliteSource

    db, service, ctx, _, _ = asyncio.run(setup(tmp_path))
    asyncio.run(service.request(ctx, "pause"))
    source = SqliteSource(db.path, "demo")
    assert source.snapshot().overview.account_id == "acct"

    class Client:
        calls = []
        revoked = []

        def connected(self):
            return True

        def revoke(self, sid):
            self.revoked.append(sid)

        def command(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return {
                **kwargs,
                "command": command,
                "state": "confirmation_required",
                "message": "<img src=x onerror=alert(1)>",
                "confirmation_id": "confirm",
                "targets": [],
                "resume": None,
            }

    trader = Client()
    auth = Auth(code=lambda: CODE, clock=lambda: 1000)
    with TestClient(create_app(source, auth, trader, port=8765), base_url=BASE) as client:
        sid = login(client)
        client.cookies.set(COOKIE, sid)
        response = client.post(
            "/control/cancel",
            headers=ORIGIN,
            data={"csrf": csrf(client), "request_id": "web-cancel"},
        )
        assert response.status_code == 200
        assert "명령 결과 - RichLetsGo DEMO" in response.text
        assert "<img src=x" not in response.text and "&lt;img" in response.text
        assert 'value="confirm"' in response.text
        assert trader.calls[0][1]["account_id"] == "acct"
        client.post("/logout", headers=ORIGIN, data={"csrf": csrf(client)})
        assert trader.revoked == [sid]
    connection = open_readonly(db.path)
    try:
        assert connection.execute("SELECT count(*) FROM commands").fetchone() == (1,)
    finally:
        connection.close()


def test_http_client_only_forwards_session_hash_and_environment_identity(monkeypatch):
    from richletsgo_dashboard.control import HttpTraderClient

    calls = []
    client = HttpTraderClient(
        port=8766,
        dashboard_port=8765,
        token=Secret("secret"),
        environment="demo",
        account_id="acct",
    )

    def call(path, body=None):
        calls.append((path, body))
        return {"environment": "demo", "account_id": "acct"}

    monkeypatch.setattr(client, "_call", call)
    assert client.connected()
    client.command(
        "pause", session_id="raw-session", environment="demo", account_id="acct", request_id="r"
    )
    assert calls[-1][1]["session_id"] == client.session_hash("raw-session")
    assert "raw-session" not in str(calls)
    client.revoke("raw-session")
    assert calls[-1][0] == "/sessions/revoke"


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("observed", [False, True])
def test_startup_merges_pauses_and_only_restores_untouched_seed(tmp_path, backend, observed):
    from richletsgo.storage.codec import encode

    async def run():
        db, service, ctx, clock, _ = await setup(tmp_path, backend)
        stored = EquityTracker.open(AccountSnapshot(NOW, D(1000), D(0)))
        stored.observe(AccountSnapshot(NOW, D(1000), D(-100)))
        pauses = PauseState((Pause(PauseKind.ENTRY_PAUSED_MANUAL, "saved", NOW),))
        async with db.unit_of_work() as uow:
            uow.recovery.put("pauses", json.dumps(pauses.to_json()))
            uow.recovery.put("equity_tracker", encode(stored.state))
            await uow.commit()
        service.pauses.add(Pause(PauseKind.HALTED, "new-live-halt", NOW))
        if observed:
            # Equal timestamp observations also count as live state and must survive.
            service.tracker.observe(AccountSnapshot(NOW, D(1000), D(-200)))
        before = service.tracker.state
        await service.startup()
        assert service.pauses.has(PauseKind.HALTED)
        assert service.pauses.has(PauseKind.ENTRY_PAUSED_MANUAL)
        assert service.tracker.state == (before if observed else stored.state)
        service.tracker.observe(AccountSnapshot(NOW + timedelta(seconds=1), D(1000), D(-250)))
        latest = service.tracker.state
        await service.startup()
        assert service.tracker.state == latest
        assert service.pauses.has(PauseKind.HALTED)

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,state", [(403, "rejected"), (503, "unavailable"), (None, "response_unknown")]
)
def test_http_error_result_and_transport_retry_screen(tmp_path, monkeypatch, status, state):
    import io
    from urllib.error import HTTPError, URLError

    from test_dashboard import BASE, CODE, ORIGIN, csrf, login

    from richletsgo_dashboard import control
    from richletsgo_dashboard.app import create_app
    from richletsgo_dashboard.security import Auth
    from richletsgo_dashboard.source import FakeSource

    class Source(FakeSource):
        def snapshot(self):
            snap = super().snapshot()
            snap.overview = replace(snap.overview, sample=False)
            return snap

    calls = []

    def urlopen(request, **kwargs):
        calls.append(json.loads(request.data))
        if status is None:
            raise URLError("synthetic timeout")
        raise HTTPError(
            request.full_url,
            status,
            "synthetic",
            {},
            io.BytesIO(
                json.dumps(
                    {"state": state, "message": "denied <img src=x onerror=alert(1)>"}
                ).encode()
            ),
        )

    monkeypatch.setattr(control, "urlopen", urlopen)
    trader = control.HttpTraderClient(
        port=8766,
        dashboard_port=8765,
        token=Secret("synthetic"),
        environment="demo",
        account_id="acct",
    )
    monkeypatch.setattr(trader, "connected", lambda: True)
    auth = Auth(code=lambda: CODE, clock=lambda: 1000)
    with TestClient(create_app(Source(), auth, trader, port=8765), base_url=BASE) as c:
        login(c)
        response = c.post(
            "/control/resolve",
            headers=ORIGIN,
            data={
                "csrf": csrf(c),
                "request_id": "same-id",
                "order_link_id": "same-order",
                "confirmation_id": "same-confirmation",
                "manually_checked": "true",
            },
        )
        assert response.status_code == 200
        text = response.text
        if status is not None:
            assert "denied &lt;img" in text and "<img src=x" not in text
            assert "응답 확인 필요" not in text
            assert '<form method="post"' not in text
        else:
            assert "응답 확인 필요" in text
            for value in ("same-id", "same-order", "same-confirmation"):
                assert f'value="{value}"' in text
            assert 'name="manually_checked" value="true"' in text
            c.post(
                "/control/resolve",
                headers=ORIGIN,
                data={
                    "csrf": csrf(c),
                    "request_id": "same-id",
                    "order_link_id": "same-order",
                    "confirmation_id": "same-confirmation",
                    "manually_checked": "true",
                },
            )
            assert calls[0] == calls[1]
