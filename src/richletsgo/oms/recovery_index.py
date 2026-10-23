"""Transactional active journal indices and completed-record retention."""

import json
from decimal import Decimal

from richletsgo.storage.contracts import UnitOfWork

from .journal import JournalRecord


def store_record(uow: UnitOfWork, record: JournalRecord) -> None:
    uow.recovery.put(record.key, record.encode())
    request_raw = uow.recovery.get(f"journal:{record.operation_id}:requested")
    result_raw = uow.recovery.get(f"journal:{record.operation_id}:result")
    if request_raw is None or result_raw is None:
        return
    request = JournalRecord.decode(request_raw)
    result = JournalRecord.decode(result_raw)
    if (
        request.op_type == "amend"
        and request.action_key
        and "qty" in request.payload()["fields"]
        and json.loads(result.result).get("code") == "UNSUPPORTED"
    ):
        uow.recovery.put("d90:" + record.operation_id, request.key)


def compact_journal(uow: UnitOfWork) -> None:
    for key, raw in uow.recovery.active("journal:"):
        if not key.endswith(":requested"):
            continue
        request = JournalRecord.decode(raw)
        result_key = f"journal:{request.operation_id}:result"
        result_raw = uow.recovery.get(result_key)
        if result_raw is None:
            continue
        if request.group_id:
            group = uow.recovery.get(request.group_id)
            if not group or not json.loads(group).get("done"):
                continue
        candidate = "d90:" + request.operation_id
        if uow.recovery.get(candidate):
            group = uow.recovery.get("replacement:" + str(request.action_key))
            if not group or not json.loads(group).get("done"):
                continue
            uow.recovery.archive(candidate)
            if not json.loads(group).get("exhausted"):
                uow.recovery.archive("replacement:" + str(request.action_key))
        outcome_key = "outcome:" + request.operation_id
        outcome = uow.recovery.get(outcome_key)
        if outcome not in (None, "UNKNOWN", "OPEN", "SUBMITTED", "CREATED"):
            pass
        else:
            result = JournalRecord.decode(result_raw)
            code = json.loads(result.result).get("code")
            order = uow.orders.find(request.target, None) or uow.orders.find(None, request.target)
            if code in ("REJECTED", "UNSUPPORTED", "NOT_FOUND", "TOO_LATE"):
                outcome = code
            elif order is not None and order.state.is_terminal:
                outcome = "TERMINAL"
            elif (
                request.op_type in ("create", "close")
                and order is not None
                and order.state.value in ("OPEN", "PARTIALLY_FILLED")
            ):
                outcome = "CONFIRMED"
            elif request.op_type == "amend" and order is not None:
                payload = request.payload()
                actual = {
                    "qty": order.qty.value,
                    "trigger": order.trigger_price.value if order.trigger_price else None,
                }
                if all(actual[f] == Decimal(payload["after"][f]) for f in payload["fields"]):
                    outcome = "APPLIED"
                else:
                    continue
            else:
                continue
            uow.recovery.put(outcome_key, outcome)
        uow.recovery.archive(key)
        uow.recovery.archive(result_key)
        uow.recovery.archive(outcome_key)
        uow.recovery.archive("trail_seen:" + request.operation_id)
