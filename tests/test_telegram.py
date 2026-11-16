"""Fake Bot API only: durable receipts, shared commands and isolated delivery."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_control import setup
from test_oms import order

from richletsgo.config import TelegramConfig
from richletsgo.domain import OrderState
from richletsgo.keystore import Secret
from richletsgo.notify.bot import BotClient, BotError
from richletsgo.notify.events import Notice
from richletsgo.notify.telegram import Telegram, expiry_text
from richletsgo.ops.logging import Redactor
from richletsgo.risk.pause import PauseKind
from richletsgo.storage.sqlite import open_readonly


class FakeBot:
    def __init__(self):
        self.updates, self.calls, self.sent = [], [], []
        self.failure = None
        self.next_message = 0

    async def call(self, method, payload):
        self.calls.append((method, payload))
        if self.failure:
            raise self.failure
        if method == "getMe":
            return {"id": 99, "username": "test_bot"}
        if method == "getUpdates":
            if "offset" in payload:
                self.updates = [v for v in self.updates if v["update_id"] >= payload["offset"]]
            return list(self.updates)
        if method == "sendMessage":
            self.next_message += 1
            self.sent.append({**payload, "message_id": self.next_message})
            return {"message_id": self.next_message}
        return True


def message(update_id, text, user=1, chat=None, private=True):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user},
            "chat": {
                "id": user if chat is None else chat,
                "type": "private" if private else "group",
            },
            "text": text,
        },
    }


def callback(update_id, nonce, message_id, user=1, chat=None):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"query{update_id}",
            "from": {"id": user},
            "data": nonce,
            "message": {
                "message_id": message_id,
                "chat": {"id": user if chat is None else chat, "type": "private"},
            },
        },
    }


async def runtime(tmp_path, backend="sqlite", **kwargs):
    db, service, ctx, clock, _ = await setup(tmp_path, backend)
    bot = FakeBot()
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    tg = Telegram(
        database=db,
        service=service,
        bot=bot,
        clock=clock,
        policy=TelegramConfig(20, 30, 1, 300, 2, 100, 1, 120),
        users=(1,),
        expires_at=clock.now() + timedelta(days=30),
        warning_window=timedelta(seconds=60),
        sleep=sleep,
        **kwargs,
    )
    service.notify = tg.command_notification
    await tg.initialize()
    return db, service, clock, bot, tg, sleeps


async def receive(bot, tg, *updates):
    bot.updates = list(updates)
    await tg.poll_once()
    await tg.process_once()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_pause_status_dedup_and_completed_receipts_not_in_working_set(tmp_path, backend):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path, backend)
        await receive(bot, tg, message(10, "/pause"))
        revision = service.pauses.revision
        await tg.send_once()
        await tg.poll_once()  # Remote acknowledgement, no repeat execution.
        await tg.process_once()
        assert service.pauses.revision == revision and len(bot.sent) == 1
        await receive(bot, tg, message(11, "/status"))
        await tg.send_once()
        assert len(bot.sent) == 2 and "daily_pnl" in bot.sent[-1]["text"]
        assert "키 만료까지" in bot.sent[-1]["text"]
        async with db.unit_of_work() as u:
            assert not u.recovery.active("telegram_inbox:")
            assert not u.recovery.active("telegram_outbox:")
            if backend == "sqlite":
                assert not any(k.startswith("telegram_inbox:") for k, _ in u.recovery.items())
        async with db.unit_of_work(recovery_keys=(tg.key("inbox", "10"),)) as u:
            assert u.recovery.get(tg.key("inbox", "10"))
        if backend == "sqlite":
            c = open_readonly(db.path)
            try:
                assert c.execute(
                    "SELECT count(*) FROM recovery_archive WHERE active!=0"
                ).fetchone() == (0,)
                assert c.execute("SELECT count(*) FROM alerts").fetchone()[0] == 2
            finally:
                c.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "update",
    [
        message(1, "/pause", user=2),
        message(1, "/pause", chat=-50, private=False),
    ],
)
def test_unauthorized_group_are_silent_and_audited(tmp_path, update):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        await receive(bot, tg, update)
        await tg.send_once()
        assert not bot.sent and not service.pauses.active()
        async with db.unit_of_work() as u:
            rows = u.recovery.active("command:")
            assert len(rows) == 1 and json.loads(rows[0][1])["state"] == "rejected"
            assert "never-store" not in str(u.recovery.items())

    asyncio.run(run())


def test_callbacks_bind_original_actor_chat_message_and_execute_once(tmp_path):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        await receive(bot, tg, message(1, "/pause"), message(2, "/resume"))
        await tg.send_once()
        prompt = next(m for m in bot.sent if "reply_markup" in m)
        nonce = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert len(nonce.encode()) <= 64 and "허용 폭" in prompt["text"]
        for update in (
            callback(3, nonce, prompt["message_id"], user=2),
            callback(4, nonce, 999),
            callback(5, nonce, prompt["message_id"], chat=2),
        ):
            await receive(bot, tg, update)
            assert service.pauses.has(PauseKind.ENTRY_PAUSED_MANUAL)
        await receive(bot, tg, callback(6, nonce, prompt["message_id"]))
        assert not service.pauses.has(PauseKind.ENTRY_PAUSED_MANUAL)
        await tg.send_once()
        count = len(bot.sent)
        await receive(bot, tg, callback(7, nonce, prompt["message_id"]))
        await tg.send_once()
        assert len(bot.sent) == count
        assert "실제 적용값" in bot.sent[-1]["text"]

    asyncio.run(run())


def test_expiry_and_restart_never_regenerate_confirmations(tmp_path):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        await receive(bot, tg, message(1, "/cancel"))
        await tg.send_once()
        prompt = bot.sent[-1]
        nonce = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        clock.advance(timedelta(seconds=61))
        await service.tick()
        await receive(bot, tg, callback(2, nonce, prompt["message_id"]))
        await tg.send_once()
        assert "새 요청 필요" in bot.sent[-1]["text"]
        bot.updates = [message(3, "/resume")]
        await tg.poll_once()  # crash after durable receipt, before command execution
        tg.boot = "new-boot"
        service.prompts.clear()
        await tg.process_once()
        await tg.send_once()
        assert not service.prompts and "새 확인 요청 필요" in bot.sent[-1]["text"]

    asyncio.run(run())


def test_resolve_is_unknown_not_found_only_and_cancel_preserves_stops(tmp_path):
    from richletsgo.domain import OrderPurpose

    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        entry = order(state=OrderState.UNKNOWN)
        stop = order(purpose=OrderPurpose.PROTECT, suffix="stop")
        async with db.unit_of_work() as u:
            u.orders.put(entry)
            u.orders.put(stop)
            await u.commit()
        await receive(bot, tg, message(1, "/cancel"))
        await tg.send_once()
        prompt = bot.sent[-1]
        nonce = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        binding = service.prompts[nonce]
        assert stop.key not in binding.targets and entry.key in binding.targets
        await receive(bot, tg, message(2, "/resolve " + entry.order_link_id))
        await tg.send_once()
        prompt = bot.sent[-1]
        nonce = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert "直接" not in prompt["text"] and "직접 확인" in prompt["text"]
        await receive(bot, tg, callback(3, nonce, prompt["message_id"]))
        async with db.unit_of_work() as u:
            assert u.orders.get(entry.key).state is OrderState.NOT_FOUND
            assert u.orders.get(stop.key).state is stop.state

    asyncio.run(run())


def test_cursor_restart_idle_lower_ids_and_payload_collision(tmp_path):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        await receive(bot, tg, message(10000, "/status"))
        await tg.poll_once()
        assert bot.calls[-1][1]["offset"] == 10001
        clock.advance(timedelta(days=8))
        bot.updates = [message(20, "/status")]
        await tg.poll_once()
        assert "offset" not in bot.calls[-1][1]
        await tg.process_once()
        bot.updates = [message(20, "/pause")]
        tg.restart_probe = True
        with pytest.raises(BotError):
            await tg.poll_once()
        await tg.process_once()
        assert not service.pauses.active()
        assert any("衝突" not in str(n) and "수동 확인" in n for _, n in tg._notices)

    asyncio.run(run())


def test_receive_commit_failure_never_acknowledges_batch(tmp_path):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        bot.updates = [message(1, "/pause")]

        def fail(stage, c):
            if stage == "before_flush":
                raise OSError("synthetic storage failure")

        db.fault = fail
        with pytest.raises(OSError):
            await tg.poll_once()
        assert not service.pauses.active()
        db.fault = None
        await tg.poll_once()
        assert "offset" not in bot.calls[-1][1]
        await tg.process_once()
        assert service.pauses.has(PauseKind.ENTRY_PAUSED_MANUAL)

    asyncio.run(run())


@pytest.mark.parametrize("error", [BotError(429, 90), BotError(403), BotError(503), BotError()])
def test_send_failure_is_durable_and_does_not_block_shared_pause(tmp_path, error):
    async def run():
        db, service, clock, bot, tg, sleeps = await runtime(tmp_path)
        await receive(bot, tg, message(1, "/pause"))
        bot.failure = error
        await tg.send_once()
        async with db.unit_of_work() as u:
            queued = u.recovery.active("telegram_outbox:")
            assert len(queued) == 1
            row = json.loads(queued[0][1])
            assert row["attempts"] == 1 and row["delivery"] in {"rejected", "response_unknown"}
            assert not u.recovery.active("telegram_inbox:")
        assert service.pauses.has(PauseKind.ENTRY_PAUSED_MANUAL)
        assert sleeps[-1] == 2
        bot.failure = None
        clock.advance(timedelta(seconds=301))
        await tg.send_once()
        assert len(bot.sent) == 1

    asyncio.run(run())


def test_persistent_warning_batching_resolution_and_secret_redaction(tmp_path):
    async def run():
        redactor = Redactor((Secret("fake-BOT-secret"), Secret("fake-API-secret")))
        db, service, clock, bot, tg, _ = await runtime(tmp_path, redactor=redactor)
        for _ in range(5):
            tg.capture(
                Notice(
                    "fake-BOT-secret fake-API-secret signature=hidden", code="clock", symbol="BTC"
                )
            )
        await tg.notices_once()
        await tg.send_once()
        assert len(bot.sent) == 1 and "fake-" not in bot.sent[0]["text"]
        clock.advance(timedelta(seconds=60))
        await tg.notices_once()  # restore durable aggregation state
        await tg.send_once()
        assert len(bot.sent) == 2 and "repeated 4" in bot.sent[-1]["text"]
        tg.capture(Notice("recovered", code="clock", symbol="BTC", resolved=True))
        await tg.notices_once()
        await tg.send_once()
        assert bot.sent[-1]["text"].endswith("recovered")
        c = open_readonly(db.path)
        try:
            content = str(c.execute("SELECT payload FROM alerts").fetchall())
            assert (
                "fake-BOT-secret" not in content
                and "fake-API-secret" not in content
                and "hidden" not in content
            )
        finally:
            c.close()

    asyncio.run(run())


@pytest.mark.parametrize("days", [15, 14, 13, 1, 0, -1])
def test_expiry_calendar_schedule_once_per_utc_day(tmp_path, days):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        tg.expires_at = clock.now() + timedelta(days=days, hours=1)
        await tg.scheduled_once()
        await tg.send_once()
        await tg.scheduled_once()
        await tg.send_once()
        assert len(bot.sent) == (1 if days <= 14 else 0)
        if days == 0:
            assert "0일" in bot.sent[0]["text"]
        if days < 0:
            assert expiry_text(clock.now(), tg.expires_at) == "만료됨"

    asyncio.run(run())


def test_daily_summary_waits_for_midnight_commit_and_sends_once(tmp_path):
    from decimal import Decimal as D

    from test_m304_trader import SYMBOLS, T, candle
    from test_m304_trader import runtime as trader_runtime

    from richletsgo.domain import StrategyId
    from richletsgo.live.trader import FundingPayment
    from richletsgo.marketdata.models import Interval
    from richletsgo.portfolio.models import StrategyBook

    async def run():
        db, clock, trader, inputs, sync, alerts = await trader_runtime(tmp_path)
        bot = FakeBot()

        async def sleep(_):
            pass

        tg = Telegram(
            database=db,
            service=trader.control,
            bot=bot,
            clock=clock,
            policy=TelegramConfig(20, 30, 1, 300, 2, 100, 1, 120),
            users=(1,),
            expires_at=T + timedelta(days=30),
            warning_window=timedelta(minutes=1),
            sleep=sleep,
        )
        await tg.initialize()
        assert await trader.boot()
        async with db.unit_of_work() as u:
            u.portfolio.put_book(StrategyBook(StrategyId.S1, SYMBOLS[0], D(1), D(100)))
            await u.commit()
        for interval in (Interval.HOUR, Interval.DAY):
            for symbol in SYMBOLS:
                await trader.accept(candle(symbol, interval))
        assert not await trader.process(T)
        await tg.scheduled_once()
        await tg.send_once()
        assert not any("일일 요약" in m["text"] for m in bot.sent)
        inputs.payments = (FundingPayment("midnight", SYMBOLS[0], T, D(1)),)
        inputs.balance = D(999)
        assert await trader.process(T)
        await tg.scheduled_once()
        await tg.send_once()
        summaries = [m for m in bot.sent if "일일 요약" in m["text"]]
        assert (
            len(summaries) == 1
            and "비활성" in summaries[0]["text"]
            and '"pnl_d81":"-1"' in summaries[0]["text"]
        )
        await tg.scheduled_once()
        await tg.send_once()
        assert len([m for m in bot.sent if "일일 요약" in m["text"]]) == 1
        await trader.close()

    asyncio.run(run())


def test_slow_bot_sender_does_not_block_actual_trader_boundary(tmp_path):
    from decimal import Decimal as D

    from test_m304_trader import SYMBOLS, T, candle, virtual_runtime

    from richletsgo.marketdata.models import Interval
    from richletsgo.risk.equity import AccountSnapshot, EquityTracker

    async def run():
        db, clock, trader, inputs, exchange, sync = await virtual_runtime(tmp_path)
        bot, entered, release = FakeBot(), asyncio.Event(), asyncio.Event()
        original = bot.call

        async def blocked(method, payload):
            if method == "sendMessage":
                entered.set()
                await release.wait()
            return await original(method, payload)

        bot.call = blocked

        async def sleep(_):
            await asyncio.sleep(0)

        tg = Telegram(
            database=db,
            service=trader.control,
            bot=bot,
            clock=clock,
            policy=TelegramConfig(20, 30, 1, 300, 2, 100, 1, 120),
            users=(1,),
            expires_at=T + timedelta(days=30),
            warning_window=timedelta(minutes=1),
            sleep=sleep,
        )
        await tg.initialize()
        sender = None
        try:
            assert await trader.boot()
            trader.gate.tracker.publish(EquityTracker.open(AccountSnapshot(T, D(1000), D(0))))
            tg.capture(Notice("startup", code="startup"))
            await tg.notices_once()
            sender = asyncio.create_task(tg.send_once())
            await asyncio.wait_for(entered.wait(), 2)
            for interval in (Interval.HOUR, Interval.DAY):
                for symbol in SYMBOLS:
                    await trader.accept(candle(symbol, interval))
            await asyncio.wait_for(trader.maintenance(), 5)
            assert await asyncio.wait_for(trader.process_next(), 5)
            async with db.unit_of_work() as u:
                assert len(u.decisions.all()) == 4 and all(
                    d.kind.value == "ADMITTED" for d in u.decisions.all()
                )
            assert not sender.done()
        finally:
            release.set()
            if sender:
                await sender
            await trader.close()

    asyncio.run(run())


def test_expiry_uses_utc_dates_across_leap_day_month_end_and_key_rotation(tmp_path):
    assert (
        expiry_text(datetime(2028, 2, 28, 23, tzinfo=UTC), datetime(2028, 3, 1, tzinfo=UTC))
        == "2일"
    )

    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        tg.expires_at = clock.now() + timedelta(days=14)
        await tg.scheduled_once()
        await tg.send_once()
        tg.expires_at += timedelta(hours=1)
        await tg.scheduled_once()
        await tg.send_once()
        assert len(bot.sent) == 2

    asyncio.run(run())


def test_fill_alert_idempotent_after_restart_and_pending_deadline_resolution(tmp_path):
    from test_oms import execution

    from richletsgo.storage.contracts import FillRecord, FillStatus

    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        fill = execution(order(), at=clock.now() - timedelta(minutes=3))
        async with db.unit_of_work() as u:
            u.fills.add(FillRecord(fill, FillStatus.PENDING_ATTRIBUTION, None, None))
            await u.commit()
        await tg.scheduled_once()
        await tg.send_once()
        assert len(bot.sent) == 2 and any("기한 초과" in m["text"] for m in bot.sent)
        tg.boot = "restart"
        await tg.scheduled_once()
        await tg.send_once()
        assert len(bot.sent) == 2
        async with db.unit_of_work() as u:
            from richletsgo.domain import StrategyId

            u.fills.replace(FillRecord(fill, FillStatus.BOOKED, None, StrategyId.S1))
            await u.commit()
        await tg.scheduled_once()
        await tg.send_once()
        assert len(bot.sent) == 3 and "보류 해소" in bot.sent[-1]["text"]

    asyncio.run(run())


def test_bounded_notice_overflow_keeps_delivery_progress(tmp_path):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        tg.policy = replace(tg.policy, queue_limit=1)
        tg.capture(Notice("one", code="one"))
        tg.capture(Notice("two", code="two"))
        assert tg.dropped == 1
        await tg.notices_once()
        await tg.send_once()
        await tg.notices_once()
        await tg.send_once()
        assert len(bot.sent) == 2 and "상한 초과" in bot.sent[-1]["text"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "error", [BotError(401), BotError(503), ValueError("malformed fake response")]
)
def test_poll_supervisor_failure_isolated_and_health_reported(tmp_path, error):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        stop = asyncio.Event()
        bot.failure = error

        async def stop_after_retry(seconds):
            assert seconds > 0
            stop.set()

        tg.sleep = stop_after_retry
        await tg.supervise("poll", tg.poll_once, stop)
        assert tg.health["poll"] == "unavailable"
        bot.failure = None
        await receive(bot, tg, message(1, "/pause"))
        assert service.pauses.has(PauseKind.ENTRY_PAUSED_MANUAL)

    asyncio.run(run())


def test_429_backoff_applies_to_other_outbox_messages(tmp_path):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        await receive(bot, tg, message(1, "/pause"), message(2, "/status"))
        bot.failure = BotError(429, 90)
        await tg.send_once()
        before = len(bot.calls)
        await tg.send_once()
        assert len(bot.calls) == before
        bot.failure = None
        clock.advance(timedelta(seconds=90))
        await tg.send_once()
        assert len(bot.sent) == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    "body,status",
    [
        (
            {
                "ok": False,
                "error_code": 429,
                "parameters": {"retry_after": 77},
                "description": "fake-token",
            },
            429,
        ),
        ({"ok": False, "description": "fake-token"}, 403),
        ([], 200),
    ],
)
def test_transport_errors_never_reveal_token_or_remote_body(body, status):
    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return body

    class Session:
        def post(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            return Response()

    Response.status = status

    async def run():
        client = BotClient(Session(), Secret("fake-token"), 30)
        with pytest.raises(BotError) as error:
            await client.call("getMe", {})
        assert "fake-token" not in str(error.value) and "fake-token" not in repr(error.value)
        if status == 429:
            assert error.value.retry_after == 77

    asyncio.run(run())


def test_cancelled_send_attempt_is_recorded_as_possible_duplicate_on_retry(tmp_path):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        await receive(bot, tg, message(1, "/status"))
        entered = asyncio.Event()
        original = bot.call

        async def blocked(method, payload):
            if method == "sendMessage":
                entered.set()
                await asyncio.Event().wait()
            return await original(method, payload)

        bot.call = blocked
        task = asyncio.create_task(tg.send_once())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with db.unit_of_work() as u:
            rows = u.recovery.active("telegram_outbox:")
            key, raw = rows[0]
            assert json.loads(raw)["delivery"] == "sending"
        bot.call = original
        await tg.send_once()
        async with db.unit_of_work(recovery_keys=(key,)) as u:
            saved = json.loads(u.recovery.get(key))
            assert saved["delivery"] == "sent" and saved["possible_duplicate"]
            assert saved["attempts"] == 2

    asyncio.run(run())


def test_critical_first_alert_precedes_older_warning_summary(tmp_path):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        tg.capture(Notice("normal", code="ordinary"))
        tg.capture(Notice("normal", code="ordinary"))
        await tg.notices_once()
        await tg.send_once()
        clock.advance(timedelta(minutes=1))
        await tg.notices_once()
        tg.capture(Notice("uncertain", code="storage_uncertain", related="txn1"))
        await tg.notices_once()
        await tg.send_once()
        assert "uncertain" in bot.sent[1]["text"] and "repeated 1" in bot.sent[2]["text"]

    asyncio.run(run())


def test_large_completed_archive_does_not_enter_sqlite_working_set(tmp_path):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        async with db.unit_of_work() as u:
            for i in range(500):
                key = tg.key("inbox", str(i))
                u.recovery.put(key, '{"input":{},"stage":"done"}')
                u.recovery.archive(key)
            await u.commit()
        async with db.unit_of_work() as u:
            assert not any(k.startswith("telegram_inbox:") for k, _ in u.recovery.items())
        async with db.unit_of_work(recovery_keys=(tg.key("inbox", "300"),)) as u:
            rows = [k for k, _ in u.recovery.items() if k.startswith("telegram_inbox:")]
            assert rows == [tg.key("inbox", "300")]

    asyncio.run(run())


def test_restart_replays_committed_cancel_result_instead_of_regenerating_prompt(tmp_path):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        async with db.unit_of_work() as u:
            u.orders.put(order())
            await u.commit()
        bot.updates = [message(1, "/cancel")]
        await tg.poll_once()
        from richletsgo.control.models import Context

        context = Context(
            "telegram", "telegram:1", "1", service.environment, service.account_id, "1"
        )
        prompt = await service.request(context, "cancel")
        result = await service.request(context, "cancel", confirmation_id=prompt["confirmation_id"])
        assert result["state"] == "accepted"
        # Domain commit succeeded; inbox completion did not. Restart must use it.
        tg.boot = "another-boot"
        service.prompts.clear()
        await tg.process_once()
        await tg.send_once()
        assert len(bot.sent) == 1 and "accepted" in bot.sent[0]["text"]
        assert not service.prompts and "새 확인 요청" not in bot.sent[0]["text"]

    asyncio.run(run())


@pytest.mark.parametrize("change", ["missing", "before", "nonutc", "users", "timeout"])
def test_telegram_required_config_and_credentials_dates(tmp_path, change):
    from test_service_config import config_file

    from richletsgo.config import ConfigError, load_config

    path = config_file(tmp_path)
    text = path.read_text(encoding="utf-8")
    replacements = {
        "missing": ("api_expires_at =", "removed_expires_at ="),
        "before": ("2027-01-26T00:00:00Z", "2026-01-26T00:00:00Z"),
        "nonutc": ("2027-01-26T00:00:00Z", "2027-01-26T00:00:00+09:00"),
        "users": ("allowed_user_ids = [1]", "allowed_user_ids = [2]"),
        "timeout": ("http_timeout_seconds = 30", "http_timeout_seconds = 20"),
    }
    old, new = replacements[change]
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
