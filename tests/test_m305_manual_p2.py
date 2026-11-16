"""Manual-check regressions on synthetic Bot/HTTP transports and isolated state."""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from test_control import setup
from test_telegram import callback, message, receive, runtime

from richletsgo.control.api import create_control_app
from richletsgo.keystore import Secret
from richletsgo.risk.equity import AccountSnapshot, EquityTracker
from richletsgo.risk.pause import Pause, PauseKind
from richletsgo.risk.resume import ResumeRejected
from richletsgo.storage.codec import decode, encode


@pytest.mark.parametrize("text", ["/start", "/resolve TEST123", "/unknown token=never-store"])
def test_allowed_bad_input_gets_invalid_guidance_and_safe_audit(tmp_path, text):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        await receive(bot, tg, message(1, text))
        await tg.send_once()
        assert len(bot.sent) == 1 and not service.pauses.active()
        assert "권한 없는" not in bot.sent[0]["text"]
        assert "직접 확인" not in bot.sent[0]["text"]
        assert "/resolve d-" in bot.sent[0]["text"]
        async with db.unit_of_work() as u:
            records = [json.loads(v) for _, v in u.recovery.active("command:")]
            assert len(records) == 1 and records[0]["reason_code"] == "invalid"
            assert "never-store" not in str(u.recovery.items())
        await tg.process_once()
        await tg.send_once()
        assert len(bot.sent) == 1

    asyncio.run(run())


@pytest.mark.parametrize("opaque_id", ["plain-123", "opaque+/=!?query", "질의 식별자"])
@pytest.mark.parametrize("missing_message_id", [False, True])
def test_expired_callback_is_expired_not_invalid_even_with_opaque_id(
    tmp_path, opaque_id, missing_message_id
):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        await receive(bot, tg, message(1, "/cancel"))
        await tg.send_once()
        prompt = bot.sent[-1]
        nonce = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        if missing_message_id:
            key = tg.key("confirm", nonce)
            async with db.unit_of_work() as u:
                binding = json.loads(u.recovery.get(key))
                binding["message_id"] = None
                u.recovery.put(key, json.dumps(binding))
                await u.commit()
        clock.advance(timedelta(seconds=61))
        await service.tick()
        update = callback(2, nonce, prompt["message_id"])
        update["callback_query"]["id"] = opaque_id
        assert tg._normalize(update)["kind"] == "callback"
        await receive(bot, tg, update)
        await tg.send_once()
        assert "확인 만료: 새 요청 필요" in bot.sent[-1]["text"]
        async with db.unit_of_work() as u:
            audit = [json.loads(v) for _, v in u.recovery.active("command:")]
            assert len(audit) == 1 and audit[0]["reason_code"] == "expired"
            assert not u.recovery.active("telegram_inbox:")
            assert not u.recovery.active("telegram_confirm:")
            assert not u.portfolio.actions()
        assert any(
            m == "answerCallbackQuery" and p["callback_query_id"] == opaque_id for m, p in bot.calls
        )

    asyncio.run(run())


def test_malformed_callback_from_allowed_user_gets_invalid_guidance(tmp_path):
    async def run():
        db, service, clock, bot, tg, _ = await runtime(tmp_path)
        update = callback(1, "not:a:nonce", 55)
        update["callback_query"]["id"] = "opaque+/="
        await receive(bot, tg, update)
        await tg.send_once()
        assert (
            len(bot.sent) == 1
            and "確認" not in bot.sent[0]["text"]
            and "확인 입력" in bot.sent[0]["text"]
        )
        async with db.unit_of_work() as u:
            row = json.loads(u.recovery.active("command:")[0][1])
            assert row["reason_code"] == "invalid" and "권한 없는" not in row["message"]

    asyncio.run(run())


@pytest.mark.parametrize("source", ["web", "telegram"])
def test_common_service_unconfirmed_equity_no_prompt_or_state_change(tmp_path, source):
    async def run():
        db, svc, ctx, clock, _ = await setup(tmp_path, source=source)
        svc.tracker = EquityTracker.unconfirmed(clock.now())
        svc.pauses.add(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "operator", clock.now()))
        before = svc.tracker.state
        result = await svc.request(ctx, "resume")
        assert result["state"] == "rejected" and result["reason_code"] == "equity_unconfirmed"
        assert result["message"] == "평가액 미확인: 재개 불가"
        assert "confirmation_id" not in result and not svc.prompts
        assert svc.tracker.state == before and svc.pauses.has(PauseKind.ENTRY_PAUSED_MANUAL)
        assert await svc.request(ctx, "resume") == result

    asyncio.run(run())


def test_telegram_unconfirmed_resume_sends_refusal_without_button(tmp_path):
    async def run():
        db, svc, clock, bot, tg, _ = await runtime(tmp_path)
        svc.tracker = EquityTracker.unconfirmed(clock.now())
        svc.pauses.add(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "operator", clock.now()))
        await receive(bot, tg, message(1, "/resume"))
        await tg.send_once()
        assert len(bot.sent) == 1 and "평가액 미확인" in bot.sent[0]["text"]
        assert "reply_markup" not in bot.sent[0] and "drawdown" not in bot.sent[0]["text"]

    asyncio.run(run())


def test_web_unconfirmed_resume_uses_same_refusal(tmp_path):
    db, svc, ctx, clock, _ = asyncio.run(setup(tmp_path))
    svc.tracker = EquityTracker.unconfirmed(clock.now())
    svc.pauses.add(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "operator", clock.now()))
    app = create_control_app(
        svc, operator_id="operator", token=Secret("dummy-secret"), port=8766, dashboard_port=8765
    )
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        response = client.post(
            "/commands/resume",
            headers={"Authorization": "Bearer dummy-secret", "Origin": "http://127.0.0.1:8765"},
            json={
                "session_id": "a" * 64,
                "environment": "demo",
                "account_id": "acct",
                "request_id": "unknown-resume",
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["state"] == "rejected" and "평가액 미확인" in result["message"]
        assert "confirmation_id" not in result and not svc.prompts


def test_resume_service_rejects_unknown_at_request_and_confirmation_but_known_zero_is_distinct(
    tmp_path,
):
    async def run():
        db, svc, ctx, clock, _ = await setup(tmp_path)
        svc.pauses.add(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "operator", clock.now()))
        resume = svc._resume(svc.pauses, svc.tracker)
        prompt = resume.request_resume(ctx.actor_id, clock.now())
        svc.tracker.publish(EquityTracker.unconfirmed(clock.now()))
        rejected = resume.confirm_prompt(prompt, ctx.actor_id, clock.now())
        assert isinstance(rejected, ResumeRejected) and "미확인" in rejected.reason
        assert svc.pauses.has(PauseKind.ENTRY_PAUSED_MANUAL)
        assert isinstance(resume.request_resume(ctx.actor_id, clock.now()), ResumeRejected)
        svc.tracker.publish(EquityTracker.open(AccountSnapshot(clock.now(), D(0), D(0))))
        assert svc.tracker.equity_confirmed
        assert not isinstance(resume.request_resume(ctx.actor_id, clock.now()), ResumeRejected)

    asyncio.run(run())


def test_unknown_flag_and_legacy_zero_restore_do_not_enable_resume(tmp_path):
    async def run():
        db, svc, ctx, clock, _ = await setup(tmp_path)
        unknown = EquityTracker.unconfirmed(clock.now() - timedelta(days=7))
        assert not EquityTracker(decode(encode(unknown.state))).equity_confirmed
        legacy = json.loads(
            encode(EquityTracker.open(AccountSnapshot(clock.now(), D(0), D(0))).state)
        )
        legacy["fields"].pop("equity_confirmed")
        assert not EquityTracker(decode(json.dumps(legacy))).equity_confirmed
        known = EquityTracker.open(AccountSnapshot(clock.now(), D(1000), D(0)))
        positive = json.loads(encode(known.state))
        positive["fields"].pop("equity_confirmed")
        assert EquityTracker(decode(json.dumps(positive))).equity_confirmed
        async with db.unit_of_work() as u:
            u.recovery.put("equity_tracker", json.dumps(legacy))
            await u.commit()
        svc.tracker = unknown
        svc._initial_tracker_state = unknown.state
        await svc.startup()
        assert not svc.tracker.equity_confirmed
        svc.pauses.add(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "operator", clock.now()))
        assert (await svc.request(ctx, "resume"))["reason_code"] == "equity_unconfirmed"

    asyncio.run(run())


def test_trader_replaces_persisted_unconfirmed_seed_only_after_account_preparation(tmp_path):
    from test_m304_trader import T
    from test_m304_trader import runtime as trader_runtime

    async def run():
        db, clock, trader, inputs, sync, alerts = await trader_runtime(tmp_path)
        trader.initialize_tracker = True
        trader.gate.tracker.publish(EquityTracker.unconfirmed(T - timedelta(days=7)))
        async with db.unit_of_work() as u:
            u.recovery.put("equity_tracker", encode(trader.gate.tracker.state))
            await u.commit()
        assert await trader.boot()
        assert not trader.can_enter()
        clock.advance(trader.policy.bar_wait)
        assert await trader.process(T)
        assert trader.gate.tracker.equity_confirmed and trader.gate.tracker.equity == D(1000)
        assert trader.gate.tracker.day_start == T
        await trader.close()

    asyncio.run(run())
