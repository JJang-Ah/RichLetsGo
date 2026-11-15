"""Durable Telegram inbox/outbox, isolated from trading and shared command execution."""

import asyncio
import hashlib
import json
import re
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from threading import RLock
from typing import Any
from uuid import uuid4

from richletsgo.config import TelegramConfig
from richletsgo.control.models import Context
from richletsgo.control.service import CommandDenied, CommandService
from richletsgo.domain import Clock
from richletsgo.ops.logging import Redactor
from richletsgo.storage.contracts import FillStatus, UnitOfWork
from richletsgo.storage.memory import InMemoryDatabase
from richletsgo.storage.sqlite import SqliteDatabase

from .batching import WarningBatcher
from .bot import Bot, BotError
from .events import Notice


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def expiry_text(now: datetime, expires: datetime) -> str:
    if now >= expires:
        return "만료됨"
    days = (expires.date() - now.date()).days
    return "오늘 만료 (0일)" if days == 0 else f"{days}일"


class Telegram:
    def __init__(
        self,
        *,
        database: InMemoryDatabase | SqliteDatabase,
        service: CommandService,
        bot: Bot,
        clock: Clock,
        policy: TelegramConfig,
        users: tuple[int, ...],
        expires_at: datetime,
        warning_window: timedelta,
        redactor: Redactor | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.db, self.service, self.bot, self.clock, self.policy = (
            database,
            service,
            bot,
            clock,
            policy,
        )
        self.users, self.expires_at, self.warning_window = users, expires_at, warning_window
        if not users or policy.queue_limit < len(users):
            raise ValueError("Telegram queue must fit all recipients")
        self.redactor, self.sleep = redactor or Redactor(), sleep
        if {f"telegram:{v}" for v in users} != {
            v for v in service.allowed_ids if v.startswith("telegram:")
        }:
            raise ValueError("Telegram actors must match shared authorization")
        self.bot_id: int | None = None
        self._initialized = False
        self.username = ""
        self.boot = uuid4().hex
        self.restart_probe = True
        self.health = {"poll": "starting", "process": "starting", "send": "starting"}
        self._notices: deque[tuple[str, Notice]] = deque()
        self._notice_lock = RLock()
        self.dropped = 0
        self._lock = asyncio.Lock()

    @property
    def namespace(self) -> str:
        if self.bot_id is None:
            raise BotError()
        return f"{self.service.environment.value}:{self.service.account_id}:{self.bot_id}:"

    def key(self, kind: str, identity: str) -> str:
        return f"telegram_{kind}:" + self.namespace + identity

    def uow(self, *keys: str) -> UnitOfWork:
        return self.db.unit_of_work(recovery_keys=keys)

    async def initialize(self) -> None:
        me = await self.bot.call("getMe", {})
        if not isinstance(me, dict) or type(me.get("id")) is not int or me["id"] <= 0:
            raise BotError()
        self.bot_id = me["id"]
        self.username = str(me.get("username", ""))
        async with self.uow() as u:
            owner = u.recovery.get("telegram_cursor:owner")
            if owner is not None and owner != self.namespace:
                raise BotError(409)  # Never reuse another bot's account cursor/command IDs.
            u.recovery.put("telegram_cursor:owner", self.namespace)
            await u.commit()
        self._initialized = True

    def capture(self, notice: Notice) -> None:
        """Thread-safe, bounded enqueue only; no Bot/DB I/O on the Trader path."""
        clean = Notice(**self.redactor.clean(notice.record()))
        with self._notice_lock:
            if len(self._notices) >= self.policy.queue_limit:
                self.dropped += 1
                return
            self._notices.append((uuid4().hex, clean))

    def _outbox_key(self, identity: str, user: int) -> str:
        return self.key("outbox", hashlib.sha256(f"{identity}:{user}".encode()).hexdigest())

    def _enqueue(
        self,
        u: UnitOfWork,
        identity: str,
        user: int,
        text: str,
        markup: dict[str, Any] | None = None,
        confirmation: str | None = None,
        priority: int = 1,
    ) -> None:
        key = self._outbox_key(identity, user)
        if u.recovery.get(key) is not None:
            return
        if user not in self.users:
            raise CommandDenied("private recipient not allowed")
        if len(u.recovery.active("telegram_outbox:")) >= self.policy.queue_limit:
            raise BotError(503)
        row = {
            "chat_id": user,
            "text": self.redactor.text(text)[:4000],
            "markup": markup,
            "confirmation": confirmation,
            "attempts": 0,
            "next": self.clock.now().isoformat(),
            "delivery": "queued",
            "priority": priority,
        }
        u.recovery.put(key, dumps(row))

    async def command_notification(self, raw: str) -> None:
        if not self._initialized:
            raise BotError()
        record = json.loads(raw)
        if record.get("source") != "telegram":
            return  # Web replies stay on the web; pause transitions are broadcast separately.
        actor = record.get("actor_id", "")
        if not re.fullmatch(r"telegram:[1-9][0-9]*", actor):
            return
        user = int(actor.split(":")[1])
        if user not in self.users or record.get("session_id") != str(user):
            return
        identity = f"command:{record.get('storage_key', record['request_id'])}:{record['state']}"
        key = self._outbox_key(identity, user)
        async with self.uow(key) as u:
            self._enqueue(u, identity, user, self.format_result(record), priority=0)
            await u.commit()

    def format_result(self, record: dict[str, Any]) -> str:
        text = f"[{self.service.environment.value}] /{record['command']} {record['state']}\n{record['message']}"
        if record.get("resume"):
            text += "\n재개 확인 (기록용 최고점·평가액·낙폭·다음 정지 수준·허용 폭):\n" + dumps(
                record["resume"]
            )
        if record.get("resume_record"):
            text += "\n실제 적용값: " + dumps(record["resume_record"])
        if record.get("targets") is not None:
            text += "\n고정 취소 대상: " + dumps(record["targets"])
        if record.get("status"):
            text += (
                "\n"
                + dumps(record["status"])
                + "\n키 만료까지: "
                + expiry_text(self.clock.now(), self.expires_at)
            )
        if record["command"] == "resolve":
            text += "\nBybit에서 해당 주문/체결을 직접 확인했고 주문이 없음을 확인합니다."
        if record.get("expires_at"):
            text += "\n확인 만료: " + record["expires_at"]
        return text

    def _normalize(self, update: Any) -> dict[str, Any]:
        if (
            not isinstance(update, dict)
            or type(update.get("update_id")) is not int
            or update["update_id"] < 0
        ):
            raise BotError()
        result: dict[str, Any] = {"update_id": update["update_id"], "kind": "ignored"}
        callback = update.get("callback_query")
        message = callback.get("message") if isinstance(callback, dict) else update.get("message")
        if not isinstance(message, dict):
            return result
        sender = callback.get("from") if isinstance(callback, dict) else message.get("from")
        if not isinstance(message, dict) or not isinstance(sender, dict):
            return result
        chat = message.get("chat", {})
        if (
            not isinstance(chat, dict)
            or type(chat.get("id")) is not int
            or type(sender.get("id")) is not int
        ):
            return result
        result.update(
            user=sender["id"],
            chat=chat["id"],
            private=chat.get("type") == "private",
            message_id=message.get("message_id"),
        )
        if isinstance(callback, dict):
            nonce, query_id = callback.get("data"), callback.get("id")
            if (
                isinstance(nonce, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", nonce)
                and isinstance(query_id, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", query_id)
            ):
                result.update(kind="callback", nonce=nonce, query_id=query_id)
            else:
                result["kind"] = "invalid"
        else:
            text = message.get("text", "")
            if not isinstance(text, str):
                return result
            match = re.fullmatch(
                r"/(status|pause|resume|cancel|resolve)(?:@([A-Za-z0-9_]+))?(?: ([db]-[A-Za-z0-9_-]{1,34}))?",
                text.strip(),
            )
            if (
                match
                and (not match[2] or match[2] == self.username)
                and ((match[1] == "resolve") == (match[3] is not None))
            ):
                result.update(kind="command", command=match[1], target=match[3])
            else:
                result.update(kind="invalid", command="unknown")
        return result

    async def poll_once(self) -> None:
        if not self._initialized:
            await self.initialize()
        cursor_key = self.key("cursor", "updates")
        async with self.uow() as u:
            cursor = json.loads(u.recovery.get(cursor_key) or "{}")
            free = self.policy.queue_limit - len(u.recovery.active("telegram_inbox:"))
            if free <= 0:
                raise BotError(503)
        if cursor.get("blocked"):
            raise BotError(409)
        probe = (
            self.restart_probe
            or not cursor.get("last_received")
            or self.clock.now() - datetime.fromisoformat(cursor["last_received"])
            >= timedelta(hours=24)
        )
        payload: dict[str, Any] = {
            "limit": min(100, free),
            "timeout": self.policy.poll_seconds,
            "allowed_updates": ["message", "callback_query"],
        }
        if not probe and "offset" in cursor:
            payload["offset"] = cursor["offset"]
        updates = await self.bot.call("getUpdates", payload)
        if not isinstance(updates, list):
            raise BotError()
        normalized = [self.redactor.clean(self._normalize(v)) for v in updates]
        keys = tuple(self.key("inbox", str(v["update_id"])) for v in normalized)
        collision = False
        async with self.uow(*keys) as u:
            new_count = sum(u.recovery.get(key) is None for key in keys)
            if new_count + len(u.recovery.active("telegram_inbox:")) > self.policy.queue_limit:
                raise BotError(503)
            for key, value in zip(keys, normalized, strict=True):
                saved = u.recovery.get(key)
                if saved is not None:
                    if json.loads(saved)["input"] != value:
                        collision = True
                    continue
                u.recovery.put(key, dumps({"input": value, "boot": self.boot, "stage": "received"}))
            if collision:
                cursor["blocked"] = True
            elif normalized:
                cursor.update(
                    offset=max(v["update_id"] for v in normalized) + 1,
                    last_received=self.clock.now().isoformat(),
                )
            u.recovery.put(cursor_key, dumps(cursor))
            await u.commit()
        self.restart_probe = False
        if collision:
            self.capture(
                Notice("Telegram update_id 충돌: 수동 확인 필요", code="telegram_id_collision")
            )
            raise BotError(409)

    def _context(self, value: dict[str, Any], request: str) -> Context:
        return Context(
            "telegram",
            f"telegram:{value['user']}",
            str(value["chat"]),
            self.service.environment,
            self.service.account_id,
            request,
        )

    async def _audit(self, value: dict[str, Any], reason: str) -> None:
        if "user" in value:
            await self.service.telegram_inspect(
                self._context(value, "audit:" + str(value["update_id"])),
                value.get("command", "unknown"),
                denial=reason,
            )

    async def process_once(self) -> None:
        if not self._initialized:
            return
        async with self._lock:
            async with self.uow() as u:
                cursor = json.loads(u.recovery.get(self.key("cursor", "updates")) or "{}")
                if cursor.get("blocked"):
                    return
                inbox = u.recovery.active("telegram_inbox:")
            for key, raw in inbox:
                if not key.startswith(self.key("inbox", "")):
                    continue
                row = json.loads(raw)
                value = row["input"]
                if value["kind"] == "ignored":
                    await self._finish(key)
                    continue
                allowed = (
                    value.get("private")
                    and value.get("user") in self.users
                    and value.get("chat") == value.get("user")
                )
                if not allowed or value["kind"] == "invalid":
                    await self._audit(value, "권한/채팅/형식 거절")
                    await self._finish(key)
                    if value["kind"] == "callback":
                        await self._ack(value)
                    continue
                if value["kind"] == "callback":
                    try:
                        await self._callback(key, value)
                    finally:
                        await self._ack(value)
                    continue
                context = self._context(value, str(value["update_id"]))
                command = value["command"]
                if command in {"resume", "cancel", "resolve"} and row["boot"] != self.boot:
                    result = await self.service.saved_result(
                        context, command, value.get("target")
                    ) or {
                        **context.identity(),
                        "command": command,
                        "state": "interrupted",
                        "message": "재시작: 새 확인 요청 필요",
                    }
                    if result["state"] == "interrupted":
                        await self._audit(value, "재시작: 확인 요청 중단")
                else:
                    # Store processing before execution. Domain writes retain the original
                    # update ID in CommandService, including retries after unknown commits.
                    async with self.uow(key) as u:
                        row["stage"] = "started"
                        u.recovery.put(key, dumps(row))
                        await u.commit()
                    try:
                        result = (
                            await self.service.telegram_inspect(context, "status")
                            if command == "status"
                            else await self.service.request(
                                context, command, target=value.get("target")
                            )
                        )
                    except CommandDenied:
                        await self._audit(value, "명령 거절")
                        await self.command_notification(
                            dumps(
                                {
                                    **context.identity(),
                                    "command": command,
                                    "state": "rejected",
                                    "message": "명령 대상/상태 확인: 새 요청 필요",
                                }
                            )
                        )
                        await self._finish(key)
                        continue
                if result["state"] in {"confirming", "unavailable"}:
                    continue
                if result["state"] == "confirmation_required":
                    nonce = result["confirmation_id"]
                    confirm_key = self.key("confirm", nonce)
                    reply_id = "prompt:" + str(value["update_id"])
                    out_key = self._outbox_key(reply_id, value["user"])
                    async with self.uow(key, confirm_key, out_key) as u:
                        u.recovery.put(
                            confirm_key,
                            dumps(
                                {
                                    "boot": self.boot,
                                    "context": context.identity(),
                                    "command": command,
                                    "target": value.get("target"),
                                    "expires": result["expires_at"],
                                    "message_id": None,
                                }
                            ),
                        )
                        self._enqueue(
                            u,
                            reply_id,
                            value["user"],
                            self.format_result(result),
                            {
                                "inline_keyboard": [
                                    [{"text": "직접 확인했습니다 · 실행", "callback_data": nonce}]
                                ]
                            },
                            nonce,
                            priority=0,
                        )
                        u.recovery.archive(key)
                        await u.commit()
                else:
                    await self.command_notification(dumps(result))
                    await self._finish(key)

    async def _finish(self, key: str) -> None:
        async with self.uow(key) as u:
            u.recovery.archive(key)
            await u.commit()

    async def _callback(self, inbox_key: str, value: dict[str, Any]) -> None:
        key = self.key("confirm", value["nonce"])
        async with self.uow(key) as u:
            binding = json.loads(u.recovery.get(key) or "null")
        owner_valid = (
            binding
            and binding["context"]["actor_id"] == f"telegram:{value['user']}"
            and binding["context"]["session_id"] == str(value["chat"])
        )
        if (
            owner_valid
            and binding["boot"] != self.boot
            and binding["message_id"] == value["message_id"]
        ):
            old_context = Context(**{**binding["context"], "environment": self.service.environment})
            saved = await self.service.saved_result(
                old_context, binding["command"], binding["target"]
            )
            if saved is not None:
                await self.command_notification(dumps(saved))
                await self._finish(inbox_key)
                return
        valid = owner_valid and binding["boot"] == self.boot
        if valid and binding["message_id"] is None:
            return  # Sender has not committed message identity yet.
        if valid:
            valid = binding["message_id"] == value["message_id"]
        if not valid:
            await self._audit(value, "확인 번호 소유권/부팅/메시지 불일치")
            await self._finish(inbox_key)
            return
        context = Context(**{**binding["context"], "environment": self.service.environment})
        try:
            result = await self.service.request(
                context,
                binding["command"],
                target=binding["target"],
                confirmation_id=value["nonce"],
                manually_checked=binding["command"] == "resolve",
            )
        except CommandDenied:
            await self._audit(value, "확인 만료/무효: 새 요청 필요")
            result = {
                **context.identity(),
                "command": binding["command"],
                "state": "rejected",
                "message": "확인 만료/무효: 새 요청 필요",
            }
            await self.command_notification(dumps(result))
            await self._finish(inbox_key)
            return
        if result["state"] in {"confirming", "unavailable"}:
            return
        await self.command_notification(dumps(result))
        async with self.uow(key, inbox_key) as u:
            u.recovery.archive(key)
            u.recovery.archive(inbox_key)
            await u.commit()

    async def _ack(self, value: dict[str, Any]) -> None:
        # Empty acknowledgements reveal no authorization/account information.
        try:
            await self.bot.call("answerCallbackQuery", {"callback_query_id": value["query_id"]})
        except (OSError, ValueError):
            pass

    async def send_once(self) -> None:
        if not self._initialized:
            return
        send_state = self.key("cursor", "send")
        async with self.uow() as u:
            send_cursor = json.loads(u.recovery.get(send_state) or "{}")
            if (
                send_cursor.get("next")
                and datetime.fromisoformat(send_cursor["next"]) > self.clock.now()
            ):
                return
            queued = u.recovery.active("telegram_outbox:")
        for key, raw in sorted(queued, key=lambda item: json.loads(item[1]).get("priority", 1)):
            if not key.startswith(self.key("outbox", "")):
                continue
            row = json.loads(raw)
            if row["chat_id"] not in self.users:
                row.update(delivery="recipient_revoked")
                async with self.uow(key) as u:
                    u.recovery.put(key, dumps(row))
                    u.recovery.archive(key)
                    await u.commit()
                continue
            if datetime.fromisoformat(row["next"]) > self.clock.now():
                continue
            if row["confirmation"]:
                confirmation_key = self.key("confirm", row["confirmation"])
                async with self.uow(confirmation_key) as u:
                    binding = json.loads(u.recovery.get(confirmation_key) or "null")
                if (
                    not binding
                    or binding["boot"] != self.boot
                    or datetime.fromisoformat(binding["expires"]) <= self.clock.now()
                ):
                    row["markup"] = None
                    row["text"] += "\n확인 만료/재시작: 새 요청 필요"
            params = {"chat_id": row["chat_id"], "text": row["text"]}
            if row["markup"]:
                params["reply_markup"] = row["markup"]
            # Commit the attempt before the external effect. A crash/cancellation
            # after this point may have sent a message, never a trading command.
            row["possible_duplicate"] = row.get("possible_duplicate", False) or row["delivery"] in {
                "sending",
                "response_unknown",
            }
            row["attempts"] += 1
            row["delivery"] = "sending"
            async with self.uow(key) as u:
                u.recovery.put(key, dumps(row))
                await u.commit()
            try:
                response = await self.bot.call("sendMessage", params)
                if not isinstance(response, dict) or type(response.get("message_id")) is not int:
                    raise BotError()
            except (OSError, ValueError) as error:
                delay = max(
                    getattr(error, "retry_after", 0),
                    min(
                        self.policy.max_retry_seconds,
                        self.policy.retry_seconds * 2 ** min(row["attempts"], 10),
                    ),
                )
                row.update(
                    next=(self.clock.now() + timedelta(seconds=delay)).isoformat(),
                    delivery="response_unknown" if not getattr(error, "code", 0) else "rejected",
                )
                if not getattr(error, "code", 0):
                    row["possible_duplicate"] = True
                async with self.uow(key) as u:
                    u.recovery.put(key, dumps(row))
                    u.recovery.put(send_state, dumps({"next": row["next"]}))
                    await u.commit()
                self.health["send"] = "unavailable"
                await self.sleep(self.policy.send_interval_seconds)
                return
            binding_key = self.key("confirm", row["confirmation"]) if row["confirmation"] else ""
            async with self.uow(key, binding_key) as u:
                if binding_key and (binding_raw := u.recovery.get(binding_key)):
                    binding = json.loads(binding_raw)
                    binding["message_id"] = response["message_id"]
                    u.recovery.put(binding_key, dumps(binding))
                row.update(delivery="sent", message_id=response["message_id"])
                u.recovery.put(key, dumps(row))
                u.recovery.archive(key)
                await u.commit()
            self.health["send"] = "ok"
            await self.sleep(self.policy.send_interval_seconds)

    async def notices_once(self) -> None:
        if not self._initialized:
            return
        with self._notice_lock:
            entries = list(self._notices)
            dropped = self.dropped
        receipt_keys = [self.key("notice", identity) for identity, _ in entries]
        batch_key = self.key("batch", "warnings")
        output: list[Notice] = []
        batch = WarningBatcher(
            self.clock,
            self.warning_window,
            lambda n: output.append(
                n if isinstance(n, Notice) else Notice(n, code="warning_summary")
            ),
        )
        consumed = 0
        async with self.uow(*receipt_keys) as u:
            saved = json.loads(u.recovery.get(batch_key) or "{}")
            batch.restore(saved.get("episodes", []))
            pending = saved.get("pending", [])
            if len(pending) < self.policy.queue_limit:
                batch.flush()
                for (_identity, notice), receipt in zip(entries, receipt_keys, strict=True):
                    if len(pending) + len(output) >= self.policy.queue_limit:
                        break
                    consumed += 1
                    if u.recovery.get(receipt):
                        continue
                    if notice.resolved:
                        batch.resolve(notice.key, notice)
                    else:
                        batch.warn(notice.key, notice)
                    u.recovery.put(receipt, "true")
                    u.recovery.archive(receipt)
                if dropped:
                    output.append(
                        Notice(
                            f"알림 큐 상한 초과: {dropped}건 미보존", code="telegram_queue_overflow"
                        )
                    )
                pending.extend(
                    {
                        "id": "notice:" + uuid4().hex,
                        "notice": n.record(),
                        "priority": 3
                        if "(repeated " in n
                        else 0
                        if n.code.startswith(
                            ("storage", "protection", "portfolio", "unknown", "funding_off_hour")
                        )
                        else 1,
                    }
                    for n in output
                )
            else:
                dropped = 0
            free = self.policy.queue_limit - len(u.recovery.active("telegram_outbox:"))
            pending.sort(key=lambda item: item.get("priority", 1))
            sent = 0
            for item in pending:
                if free < len(self.users):
                    break
                for user in self.users:
                    self._enqueue(
                        u,
                        item["id"],
                        user,
                        f"[{self.service.environment.value}] {item['notice']['message']}",
                        priority=item.get("priority", 1),
                    )
                free -= len(self.users)
                sent += 1
            u.recovery.put(
                batch_key, dumps({"episodes": batch.snapshot(), "pending": pending[sent:]})
            )
            await u.commit()
        with self._notice_lock:
            for _ in range(consumed):
                self._notices.popleft()
            self.dropped -= dropped

    async def scheduled_once(self) -> None:
        if not self._initialized:
            return
        day = self.clock.now().date()
        identity = f"expiry:{self.expires_at.isoformat()}:{day.isoformat()}"
        async with self.uow() as u:
            fill_ids = tuple(f.event.exec_id for f in u.fills.all())
        keys = tuple(self._outbox_key(identity, user) for user in self.users) + tuple(
            self._outbox_key(kind + ":" + execution, user)
            for execution in fill_ids
            for kind in ("execution", "pending", "pending_resolved")
            for user in self.users
        )
        observation = self.key("cursor", "pauses")
        async with self.service.lock:
            async with self.uow(*keys) as u:

                def capacity() -> bool:
                    return self.policy.queue_limit - len(
                        u.recovery.active("telegram_outbox:")
                    ) >= len(self.users)

                if (self.expires_at.date() - day).days <= 14 and capacity():
                    for user in self.users:
                        self._enqueue(
                            u,
                            identity,
                            user,
                            "Bybit API 키 만료까지: "
                            + expiry_text(self.clock.now(), self.expires_at),
                            priority=3,
                        )
                for summary_key, raw in u.recovery.active("daily_summary:"):
                    if not capacity():
                        break
                    summary = json.loads(raw)
                    boundary = datetime.fromisoformat(summary["boundary"])
                    if (
                        u.recovery.get("boundary_checkpoint:" + boundary.isoformat() + ":account")
                        is None
                    ):
                        continue
                    for user in self.users:
                        self._enqueue(
                            u, summary_key, user, "일일 요약\n" + dumps(summary), priority=3
                        )
                    u.recovery.archive(summary_key)
                saved = json.loads(u.recovery.get(observation) or '{"values":{},"sequence":0}')
                previous = saved["values"]
                current = {
                    dumps(
                        [
                            p.kind.value,
                            p.reason,
                            p.symbol.value if p.symbol else "",
                            p.strategy.value if p.strategy else "",
                        ]
                    ): "정지: " + p.kind.value + " " + p.reason
                    for p in self.service.pauses.active()
                }
                transitions = [
                    ("pause:", k, current[k]) for k in current.keys() - previous.keys()
                ] + [
                    ("unpause:", k, "정지 해소: " + previous[k])
                    for k in previous.keys() - current.keys()
                ]
                if len(transitions) * len(self.users) <= self.policy.queue_limit - len(
                    u.recovery.active("telegram_outbox:")
                ):
                    sequence = saved["sequence"] + bool(transitions)
                    for kind, pause_key, text in transitions:
                        for user in self.users:
                            self._enqueue(
                                u, kind + str(sequence) + ":" + pause_key, user, text, priority=0
                            )
                    u.recovery.put(observation, dumps({"values": current, "sequence": sequence}))
                for fill in u.fills.all():
                    if fill.event.exec_id not in fill_ids:
                        continue
                    if not capacity():
                        break
                    event = fill.event
                    text = f"체결/손절: {event.exec_id} {event.symbol.value} 수량 {event.qty.value} 체결가 {event.price.value}"
                    for user in self.users:
                        self._enqueue(u, "execution:" + event.exec_id, user, text)
                    if not capacity():
                        break
                    if (
                        fill.status is FillStatus.PENDING_ATTRIBUTION
                        and self.clock.now() - event.exec_time
                        >= timedelta(seconds=self.policy.pending_alert_seconds)
                    ):
                        for user in self.users:
                            self._enqueue(
                                u,
                                "pending:" + event.exec_id,
                                user,
                                "귀속 보류 기한 초과: " + event.exec_id,
                            )
                    elif fill.status is FillStatus.BOOKED:
                        for user in self.users:
                            if u.recovery.get(self._outbox_key("pending:" + event.exec_id, user)):
                                self._enqueue(
                                    u,
                                    "pending_resolved:" + event.exec_id,
                                    user,
                                    "귀속 보류 해소: " + event.exec_id,
                                )
                for confirm_key, raw in u.recovery.active("telegram_confirm:"):
                    binding = json.loads(raw)
                    if (
                        binding["boot"] != self.boot
                        or datetime.fromisoformat(binding["expires"]) <= self.clock.now()
                    ):
                        u.recovery.archive(confirm_key)
                await u.commit()

    async def supervise(
        self, name: str, operation: Callable[[], Awaitable[None]], stop: asyncio.Event
    ) -> None:
        failures = 0
        while not stop.is_set():
            try:
                await operation()
                if name != "send":
                    self.health[name] = "ok"
                failures = 0
                delay = self.policy.send_interval_seconds if name != "poll" else 0
            except Exception as error:
                failures += 1
                self.health[name] = "unavailable"
                delay = min(
                    self.policy.max_retry_seconds,
                    self.policy.retry_seconds * 2 ** min(failures, 10),
                )
                delay = max(delay, getattr(error, "retry_after", 0))
            await self.sleep(delay)

    async def work_once(self) -> None:
        await self.process_once()
        await self.notices_once()
        await self.scheduled_once()

    async def run(self, stop: asyncio.Event) -> None:
        # Each supervisor survives transport/data errors. Trading owns neither task.
        tasks = [
            asyncio.create_task(self.supervise(name, op, stop))
            for name, op in (
                ("poll", self.poll_once),
                ("process", self.work_once),
                ("send", self.send_once),
            )
        ]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        self.capture(Notice("Trader 종료", code="trader_stopped"))
        try:
            async with asyncio.timeout(self.policy.shutdown_seconds):
                await self.notices_once()
                await self.send_once()
        except (OSError, ValueError, TimeoutError):
            pass
