"""D108 presentation contracts, with no external services."""

import ast
import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from test_telegram import runtime

from richletsgo.notify import wording as w
from richletsgo.notify.events import Notice
from richletsgo.risk.pause import Pause, PauseKind


def test_producer_vocabulary_is_registered():
    root = Path(__file__).resolve().parents[1] / "src"
    codes = {"boundary_query", "data", "ALERT", "PAUSE"}
    for path in root.rglob("*.py"):
        for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(call, ast.Call):
                continue
            name = (
                call.func.attr
                if isinstance(call.func, ast.Attribute)
                else getattr(call.func, "id", "")
            )
            nodes = []
            if name in {"pause", "_bad"}:
                nodes = call.args[:1]
            if name == "Pause":
                nodes = call.args[1:2]
            if name == "Notice":
                nodes = [k.value for k in call.keywords if k.arg == "code"]
            for node in nodes:
                if isinstance(node, ast.Subscript):
                    continue  # Restored pause row: row["reason"] is a field, not a reason.
                for val in ast.walk(node):
                    if (
                        isinstance(val, ast.Constant)
                        and isinstance(val.value, str)
                        and not val.value.endswith(":")
                    ):
                        codes.add(val.value.split(":")[-1])
    assert codes <= w.WORDS.keys(), codes - w.WORDS.keys()


@pytest.mark.parametrize("ready", [True, False])
def test_boot_sends_one_result_and_suppresses_setup_causes(tmp_path, ready):
    async def run():
        _, service, clock, bot, tg, _ = await runtime(tmp_path)
        tg.startup_ready = lambda: ready and clock.now() - tg.started_at >= timedelta(seconds=6)
        service.pauses.add(Pause(PauseKind.ENTRY_PAUSED_SYSTEM, "bybit_stream", clock.now()))
        for code in ("trader_started", "bybit:bybit_stream", "protection:sync"):
            tg.capture(Notice("remote secret must not appear", code=code))
        await tg.work_once()
        await tg.send_once()
        assert not bot.sent
        clock.advance(timedelta(seconds=6 if ready else 120))
        if ready:
            service.pauses.clear(PauseKind.ENTRY_PAUSED_SYSTEM, reason="bybit_stream")
        for _ in range(3):
            await tg.work_once()
            await tg.send_once()
        assert len(bot.sent) == 1
        assert ("시작 완료" if ready else "아직 준비 안 됨") in bot.sent[0]["text"]

    asyncio.run(run())


def test_unknown_notice_and_kst_midnight():
    assert (
        w.warning("demo", {"code": "new_reason", "message": "private exception"})
        == "⚠️ [데모] 알 수 없는 상태 (코드 new_reason)"
    )
    assert w.stamp("2026-11-18T15:04:00+00:00") == "11-19 00:04"


def test_limit_is_enforced_on_delivery(tmp_path):
    async def run():
        _, _, _, bot, tg, _ = await runtime(tmp_path)
        async with tg.uow() as u:
            tg._enqueue(u, "long", 1, "경고" * 3000)
            await u.commit()
        await tg.send_once()
        assert 0 < len(bot.sent[0]["text"]) <= 4096

    asyncio.run(run())


def test_remaining_pause_on_delayed_recovery(tmp_path):
    async def run():
        _, service, clock, bot, tg, _ = await runtime(tmp_path)
        service.pauses.add(Pause(PauseKind.ENTRY_PAUSED_SYSTEM, "query", clock.now()))
        await tg.scheduled_once()
        await tg.send_once()
        service.pauses.clear(PauseKind.ENTRY_PAUSED_SYSTEM, reason="query")
        await tg.scheduled_once()
        service.pauses.add(Pause(PauseKind.ENTRY_PAUSED_MANUAL, "operator", clock.now()))
        clock.advance(timedelta(minutes=1))
        await tg.scheduled_once()
        await tg.send_once()
        restored = next(m["text"] for m in bot.sent if "멈춤 원인 해결" in m["text"])
        assert "남은 멈춤: 수동 정지" in restored and "새 진입 가능" not in restored

    asyncio.run(run())
