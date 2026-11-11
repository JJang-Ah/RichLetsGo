"""One transactional command path for web and Telegram (D106).

The runtime must share account_lock with risk-state publication/market observations.
Exchange calls remain the responsibility of the existing outbox/synchronizer.
"""

import asyncio
import copy
import json
import secrets
from collections.abc import Awaitable, Callable, Collection
from datetime import timedelta
from typing import Any

from richletsgo.backtest.codec import primitive
from richletsgo.domain import Clock, Environment, OrderPurpose, OrderState
from richletsgo.oms.journal import JournalRecord
from richletsgo.oms.models import OrderRecord, confirm_not_found
from richletsgo.portfolio.models import Action, ActionKind
from richletsgo.risk.equity import EquityTracker
from richletsgo.risk.guards import authorize_command
from richletsgo.risk.limits import RiskLimits
from richletsgo.risk.pause import Pause, PauseKind, PauseState
from richletsgo.risk.resume import ResumeRejected, ResumeService
from richletsgo.storage.codec import decode, encode
from richletsgo.storage.contracts import UnitOfWork
from richletsgo.storage.errors import CommitOutcomeUnknown

from .models import COMMANDS, CommandPolicy, Confirmation, Context


class CommandDenied(ValueError):
    pass


class CommandService:
    def __init__(
        self,
        *,
        account_id: str,
        environment: Environment,
        uow_factory: Callable[[], UnitOfWork],
        pauses: PauseState,
        tracker: EquityTracker,
        limits: RiskLimits,
        allowed_ids: Collection[str],
        clock: Clock,
        policy: CommandPolicy,
        account_lock: asyncio.Lock,
        notify: Callable[[str], Awaitable[None]],
        resolve_commit: Callable[[str], Awaitable[bool]],
    ) -> None:
        self.account_id, self.environment = account_id, environment
        self.uow, self.pauses, self.tracker, self.limits = uow_factory, pauses, tracker, limits
        self.allowed_ids, self.clock, self.policy = frozenset(allowed_ids), clock, policy
        if any(not v.startswith(("telegram:", "web:")) for v in self.allowed_ids):
            raise ValueError("source-qualified allowed actors required")
        self.lock, self.notify, self.resolve_commit = account_lock, notify, resolve_commit
        self._notifications = asyncio.Lock()
        self.prompts: dict[str, Confirmation] = {}
        self.revoked: set[tuple[str, str]] = set()
        self.pending: dict[
            str, tuple[str, dict[str, Any], PauseState, EquityTracker, int, Any]
        ] = {}

    def _authorize(self, context: Context, command: str) -> None:
        if command not in COMMANDS:
            raise CommandDenied("unknown command")
        if context.environment is not self.environment or context.account_id != self.account_id:
            raise CommandDenied("environment/account mismatch")
        if (context.source, context.session_id) in self.revoked:
            raise CommandDenied("session revoked")
        if not authorize_command(
            context.actor_id, "/" + command, self.allowed_ids, self.clock.now()
        ).allowed:
            raise CommandDenied("actor not allowed")

    @staticmethod
    def _matches(
        record: dict[str, Any], context: Context, command: str, target: str | None
    ) -> None:
        if any(record.get(k) != v for k, v in context.identity().items()):
            raise CommandDenied("request belongs to another context")
        if record.get("command") != command or record.get("target") != target:
            raise CommandDenied("request command/target mismatch")

    def _resume(self, pauses: PauseState, tracker: EquityTracker) -> ResumeService:
        return ResumeService(
            pauses=pauses,
            tracker=tracker,
            limits=self.limits,
            allowed_ids=self.allowed_ids,
            ttl=timedelta(seconds=60),
            id_factory=lambda: secrets.token_urlsafe(32),
        )

    @staticmethod
    def _version(uow: UnitOfWork, order: OrderRecord) -> tuple[str, int]:
        count = sum(
            1
            for logged in uow.order_events.all()
            if (order.order_link_id and logged.event.order_link_id == order.order_link_id)
            or (
                order.exchange_order_id
                and logged.event.exchange_order_id == order.exchange_order_id
            )
        )
        return order.state.value, count

    def _record(
        self,
        context: Context,
        command: str,
        target: str | None,
        state: str,
        message: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            **context.identity(),
            "command": command,
            "target": target,
            "state": state,
            "message": message,
            "at": self.clock.now().isoformat(),
            **extra,
        }

    def _save(self, uow: UnitOfWork, record: dict[str, Any]) -> None:
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True)
        uow.recovery.put("command:" + record["request_id"], raw)
        # Durable delivery queue; notification I/O never runs under the database lock.
        uow.recovery.put("notification:" + record["request_id"] + ":" + record["state"], raw)

    async def request(
        self,
        context: Context,
        command: str,
        *,
        target: str | None = None,
        confirmation_id: str | None = None,
        manually_checked: bool = False,
    ) -> dict[str, Any]:
        async with self.lock:
            self._authorize(context, command)
            result = await self._request(
                context, command, target, confirmation_id, manually_checked
            )
        await self.deliver_notifications()
        return result

    async def _request(
        self,
        context: Context,
        command: str,
        target: str | None,
        confirmation_id: str | None,
        manually_checked: bool,
    ) -> dict[str, Any]:
        if context.request_id in self.pending:
            return await self._resolve(context, command, target)
        prompt = self.prompts.get(confirmation_id or "")
        async with self.uow() as uow:
            saved = uow.recovery.get("command:" + context.request_id)
            if saved:
                record: dict[str, Any] = json.loads(saved)
                self._matches(record, context, command, target)
                return record
            if uow.entry_paused:
                return self._record(
                    context, command, target, "unavailable", "저장소 복구 후 다시 확인하세요"
                )
            if confirmation_id:
                if (
                    prompt is None
                    or prompt.context != context
                    or prompt.command != command
                    or prompt.target != target
                ):
                    raise CommandDenied("confirmation binding mismatch")
                if prompt.result:
                    return prompt.result
            if command != "pause" and not confirmation_id:
                for existing in self.prompts.values():
                    if existing.context.request_id == context.request_id:
                        if (
                            existing.context != context
                            or existing.command != command
                            or existing.target != target
                        ):
                            raise CommandDenied("request confirmation mismatch")
                        return self._prompt_view(existing)
                return self._create_prompt(uow, context, command, target)

            staged_pauses, staged_tracker = (
                copy.deepcopy(self.pauses),
                EquityTracker(self.tracker.state),
            )
            before_revision, before_tracker = self.pauses.revision, self.tracker.state
            record = self._record(context, command, target, "completed", "처리 완료")
            if prompt and self.clock.now() >= prompt.expires_at:
                record.update(state="rejected", message="확인 만료: 새로 요청하세요")
            elif command == "pause":
                if confirmation_id:
                    raise CommandDenied("pause does not accept a confirmation")
                staged_pauses.add(
                    Pause(PauseKind.ENTRY_PAUSED_MANUAL, "operator", self.clock.now())
                )
                record["message"] = "신규 진입 정지"
            elif command == "resume":
                assert prompt and prompt.resume
                result = self._resume(staged_pauses, staged_tracker).confirm_prompt(
                    prompt.resume, context.actor_id, self.clock.now()
                )
                if isinstance(result, ResumeRejected):
                    record.update(state="rejected", message=result.reason)
                else:
                    record["resume_record"] = primitive(result)
                    uow.recovery.put("resume_record:" + context.request_id, encode(result))
            elif command == "resolve":
                assert prompt
                order = uow.orders.find(target, None)
                if not manually_checked:
                    record.update(state="rejected", message="거래소 내역을 직접 확인해야 합니다")
                elif (
                    order is None
                    or self._version(uow, order) != prompt.version
                    or order.state is not OrderState.UNKNOWN
                ):
                    record.update(state="rejected", message="주문 상태 변경: 새로 확인하세요")
                else:
                    resolved, audit = confirm_not_found(order, context.actor_id, self.clock.now())
                    uow.orders.put(resolved)
                    record["resolution"] = primitive(audit)
            elif command == "cancel":
                assert prompt
                targets = []
                for key in prompt.targets:
                    order = uow.orders.get(key)
                    if order is None:
                        targets.append(
                            {
                                "order_key": key,
                                "state": "failed",
                                "attempts": 0,
                                "reason": "대상 주문 없음",
                                "final_status": None,
                            }
                        )
                        continue
                    item: dict[str, Any] = {
                        "order_key": key,
                        "order_link_id": order.order_link_id,
                        "state": "pending",
                        "attempts": 0,
                        "reason": "",
                        "final_status": None,
                    }
                    if order.state.is_terminal:
                        self._terminal(item, order)
                    else:
                        self._enqueue(uow, context.request_id, item, order)
                    targets.append(item)
                record.update(targets=targets, state="accepted", message="취소 확인 중")
                self._finish(record)
            if record["state"] != "rejected" and command in ("pause", "resume"):
                uow.recovery.put("pauses", json.dumps(staged_pauses.to_json()))
                uow.recovery.put("pause_revision", str(staged_pauses.revision))
                uow.recovery.put("equity_tracker", encode(staged_tracker.state))
            self._save(uow, record)
            try:
                await uow.commit()
            except CommitOutcomeUnknown as error:
                self.pending[context.request_id] = (
                    error.txn_id,
                    record,
                    staged_pauses,
                    staged_tracker,
                    before_revision,
                    before_tracker,
                )
                return {**record, "state": "confirming", "message": "결과 확인 중"}
            if record["state"] == "completed" and command in ("pause", "resume"):
                self._publish(staged_pauses, staged_tracker, before_revision, before_tracker)
            if prompt:
                prompt.result = record
            return record

    def _publish(
        self, pauses: PauseState, tracker: EquityTracker, revision: int, prior: Any
    ) -> None:
        if self.pauses.revision != revision or self.tracker.state != prior:
            # A coordinator violating the shared-lock contract cannot silently lose newer risk state.
            self.pauses.add(
                Pause(PauseKind.ENTRY_PAUSED_SYSTEM, "control_state_reconcile", self.clock.now())
            )
            return
        self.pauses.publish(pauses)
        self.tracker.publish(tracker)

    def _create_prompt(
        self, uow: UnitOfWork, context: Context, command: str, target: str | None
    ) -> dict[str, Any]:
        prompt = Confirmation(
            secrets.token_urlsafe(32),
            context,
            command,
            target,
            self.clock.now() + timedelta(seconds=60),
        )
        if command == "resume":
            resume = self._resume(self.pauses, self.tracker).request_resume(
                context.actor_id, self.clock.now()
            )
            if isinstance(resume, ResumeRejected):
                raise CommandDenied(resume.reason)
            prompt.resume = resume
        elif command == "cancel":
            prompt.targets = tuple(
                o.key
                for o in uow.orders.all()
                if o.purpose is OrderPurpose.ENTRY and not o.state.is_terminal and o.reserved_qty
            )
        elif command == "resolve":
            order = uow.orders.find(target, None)
            if order is None or order.state is not OrderState.UNKNOWN:
                raise CommandDenied("resolve requires a specific UNKNOWN orderLinkId")
            prompt.version = self._version(uow, order)
        self.prompts[prompt.confirmation_id] = prompt
        return self._prompt_view(prompt)

    @staticmethod
    def _prompt_view(prompt: Confirmation) -> dict[str, Any]:
        return {
            **prompt.context.identity(),
            "command": prompt.command,
            "target": prompt.target,
            "state": "confirmation_required",
            "confirmation_id": prompt.confirmation_id,
            "expires_at": prompt.expires_at.isoformat(),
            "targets": list(prompt.targets),
            "resume": primitive(prompt.resume),
            "message": f"아래 {len(prompt.targets)}건만 취소합니다"
            if prompt.command == "cancel"
            else "내용을 확인한 뒤 실행하세요",
        }

    async def _resolve(self, context: Context, command: str, target: str | None) -> dict[str, Any]:
        txn, record, pauses, tracker, revision, prior = self.pending[context.request_id]
        self._matches(record, context, command, target)
        try:
            committed = await self.resolve_commit(txn)
        except (OSError, TimeoutError):
            return {**record, "state": "confirming", "message": "결과 확인 중"}
        async with self.uow() as uow:
            if committed:
                raw = uow.recovery.get("command:" + context.request_id)
                if raw is None:
                    raise RuntimeError("committed command missing")
                record = json.loads(raw)
            else:
                record = {
                    **record,
                    "state": "rejected",
                    "message": "저장 실패: 새로 요청하세요",
                    "targets": [],
                }
                self._save(uow, record)
                try:
                    await uow.commit()
                except CommitOutcomeUnknown as error:
                    self.pending[context.request_id] = (
                        error.txn_id,
                        record,
                        pauses,
                        tracker,
                        revision,
                        prior,
                    )
                    return {**record, "state": "confirming", "message": "결과 확인 중"}
        if committed and record["state"] == "completed" and command in ("pause", "resume"):
            self._publish(pauses, tracker, revision, prior)
        del self.pending[context.request_id]
        for prompt in self.prompts.values():
            if prompt.context == context:
                prompt.result = record
        return record

    def _enqueue(
        self, uow: UnitOfWork, request_id: str, target: dict[str, Any], order: OrderRecord
    ) -> None:
        attempt = target["attempts"] + 1
        key = f"cmd:{request_id}:{order.order_link_id or order.key}"
        if attempt > 1:
            key += f":retry:{attempt}"
        uow.portfolio.emit(
            Action(key, ActionKind.CANCEL_ENTRY, order.strategy, order.symbol, order_key=order.key)
        )
        target.update(
            state="pending",
            attempts=attempt,
            action_key=key,
            attempted_at=self.clock.now().isoformat(),
        )

    @staticmethod
    def _terminal(target: dict[str, Any], order: OrderRecord) -> None:
        target.update(
            state="confirmed",
            final_status=order.state.value,
            reason="취소 전에 체결됨" if order.state is OrderState.FILLED else "주문 종결 확인",
        )

    @staticmethod
    def _finish(record: dict[str, Any]) -> None:
        if all(t["state"] in ("confirmed", "failed") for t in record["targets"]):
            record["state"] = "completed"
            record["message"] = (
                "대상별 처리 종료 (실패 포함)"
                if any(t["state"] == "failed" for t in record["targets"])
                else "대상 주문 종결 확인"
            )

    async def tick(self) -> None:
        """Call after synchronizer WS/REST reconciliation; acknowledgement is never success."""
        from datetime import datetime

        async with self.lock:
            async with self.uow() as uow:
                actions = {a.key for a in uow.portfolio.actions()}
                for _, raw in uow.recovery.active("command:"):
                    record = json.loads(raw)
                    if record["command"] != "cancel" or record["state"] != "accepted":
                        continue
                    for target in record["targets"]:
                        if target["state"] in ("confirmed", "failed"):
                            continue
                        order = uow.orders.get(target["order_key"])
                        if order and order.state.is_terminal:
                            self._terminal(target, order)
                            uow.portfolio.acknowledge(target["action_key"])
                            continue
                        key = target["action_key"]
                        if key in actions:
                            continue
                        target["state"] = "requested"
                        outcome = uow.recovery.get("action_result:" + key)
                        code = (
                            json.loads(JournalRecord.decode(outcome).result).get("code")
                            if outcome
                            else None
                        )
                        if code in ("REJECTED", "UNSUPPORTED"):
                            target.update(
                                state="failed",
                                reason="거래소 영구 거절",
                                final_status=order.state.value if order else None,
                            )
                        elif (
                            self.clock.now() - datetime.fromisoformat(target["attempted_at"])
                            >= self.policy.cancel_retry_after
                        ):
                            if (
                                target["attempts"] >= self.policy.cancel_max_attempts
                                or order is None
                            ):
                                target.update(
                                    state="failed",
                                    reason="취소 확인 실패",
                                    final_status=order.state.value if order else None,
                                )
                            else:
                                self._enqueue(uow, record["request_id"], target, order)
                    self._finish(record)
                    # Save progress every time; enqueue a final notification only at completion.
                    uow.recovery.put(
                        "command:" + record["request_id"], json.dumps(record, ensure_ascii=False)
                    )
                    if record["state"] == "completed":
                        self._save(uow, record)
                await uow.commit()
        await self.deliver_notifications()

    async def revoke(self, source: str, session_id: str) -> None:
        async with self.lock:
            self.revoked.add((source, session_id))
            self.prompts = {
                k: p
                for k, p in self.prompts.items()
                if (p.context.source, p.context.session_id) != (source, session_id)
            }

    async def startup(self) -> None:
        """Invalidate confirmations and mark anomalous commands interrupted; never replay resume."""
        async with self.lock:
            self.prompts.clear()
            async with self.uow() as uow:
                saved_pauses = uow.recovery.get("pauses")
                saved_tracker = uow.recovery.get("equity_tracker")
                restored_pauses = (
                    PauseState.from_json(
                        json.loads(saved_pauses),
                        revision=int(uow.recovery.get("pause_revision") or "0"),
                    )
                    if saved_pauses
                    else None
                )
                restored_tracker = EquityTracker(decode(saved_tracker)) if saved_tracker else None
                for _, raw in uow.recovery.active("command:"):
                    record = json.loads(raw)
                    if record["state"] not in ("completed", "rejected", "interrupted") and not (
                        record["command"] == "cancel" and record["state"] == "accepted"
                    ):
                        record.update(state="interrupted", message="새 확인 필요")
                        self._save(uow, record)
                await uow.commit()
                if restored_pauses:
                    self.pauses.publish(restored_pauses)
                if restored_tracker:
                    self.tracker.publish(restored_tracker)
        await self.tick()

    async def deliver_notifications(self) -> None:
        if self.uow().entry_paused:
            return  # retain the delivery queue until storage reconciliation succeeds
        async with self._notifications:
            async with self.lock:
                async with self.uow() as uow:
                    queued = uow.recovery.active("notification:")
            for key, raw in queued:
                try:
                    await self.notify(raw)
                except (OSError, TimeoutError):
                    continue
                async with self.lock:
                    async with self.uow() as uow:
                        uow.recovery.archive(key)
                        await uow.commit()
