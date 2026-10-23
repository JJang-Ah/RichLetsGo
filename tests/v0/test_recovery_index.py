"""Bounded active recovery work, archival rollback, and replay invariants."""

import asyncio
import json
from dataclasses import replace
from decimal import Decimal as D

from test_risk import NOW
from test_trailing_stop import account, restart, target

from richletsgo.domain import Environment, Price
from richletsgo.oms.journal import JournalRecord
from richletsgo.oms.recovery_index import compact_journal, store_record
from richletsgo.portfolio.trailing import TrailRecovery, recoveries, settle_trails
from richletsgo.storage.memory import InMemoryDatabase, _Recovery


def test_archive_and_index_are_transactional_and_identical_replay_stays_archived():
    async def scenario():
        db = InMemoryDatabase("a", Environment.BACKTEST)
        async with db.unit_of_work() as u:
            u.recovery.put("trail:one", "old")
            await u.commit()
        async with db.unit_of_work() as u:
            u.recovery.archive("trail:one")
        async with db.unit_of_work() as u:
            assert u.recovery.active("trail:") == (("trail:one", "old"),)
            u.recovery.archive("trail:one")
            await u.commit()
        async with db.unit_of_work() as u:
            u.recovery.put("trail:one", "new")
            u.recovery.put("trail:two", "new")
        async with db.unit_of_work() as u:
            assert not u.recovery.active("trail:")
            assert u.recovery.get("trail:one") == "old"
            assert u.recovery.get("trail:two") is None
            u.recovery.put("trail:one", "old")
            await u.commit()
        async with db.unit_of_work() as u:
            assert not u.recovery.active("trail:")
            assert u.recovery.items() == (("trail:one", "old"),)

    asyncio.run(scenario())


def test_thousand_completed_records_do_not_increase_tick_decodes_or_active_scans(
    tmp_path, monkeypatch
):
    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path)
        await target(db, svc, "95")
        await svc.tick()
        await sync.tick()
        await sync.tick()
        async with db.unit_of_work() as u:
            trail = recoveries(u, include_archived=True)[0]
            records = [
                JournalRecord.decode(v) for k, v in u.recovery.items() if k.startswith("journal:")
            ]
            assert not u.recovery.active("trail:")
            assert not u.recovery.active("journal:")
        counts = {"trail": 0, "journal": 0, "rows": 0}
        trail_decode = TrailRecovery.decode.__func__
        journal_decode = JournalRecord.decode.__func__
        active = _Recovery.active

        def td(cls, raw):
            counts["trail"] += 1
            return trail_decode(cls, raw)

        def jd(cls, raw):
            counts["journal"] += 1
            return journal_decode(cls, raw)

        def indexed(self, prefix):
            rows = active(self, prefix)
            counts["rows"] += len(rows)
            return rows

        def forbidden(self):
            raise AssertionError("tick must not scan the audit archive")

        monkeypatch.setattr(TrailRecovery, "decode", classmethod(td))
        monkeypatch.setattr(JournalRecord, "decode", classmethod(jd))
        monkeypatch.setattr(_Recovery, "active", indexed)
        monkeypatch.setattr(_Recovery, "items", forbidden)
        await sync.tick()
        before = dict(counts)
        async with db.unit_of_work() as u:
            for i in range(1000):
                old = replace(trail, target=Price(D(80) + D(i) / D(1000)))
                u.recovery.put(old.key, old.encode())
                for record in records:
                    store_record(
                        u, replace(record, operation_id=f"completed-{i}-{record.operation_id}")
                    )
            settle_trails(u, clock.now(), svc.policy)
            compact_journal(u)
            assert not u.recovery.active("trail:")
            assert not u.recovery.active("journal:")
            await u.commit()
        for key in counts:
            counts[key] = 0
        archive = db._state.recovery_archive
        await sync.tick()
        assert db._state.recovery_archive is archive
        assert counts == before
        # Full disk replay must not revive archived attempts or resend amendments.
        sync = await restart(db, clock, svc, router, ex, sync, tmp_path)
        async with db.unit_of_work() as u:
            assert not u.recovery.active("trail:")
            assert u.recovery.get(trail.key) == trail.encode()
        await sync.close()

    asyncio.run(scenario())


def test_incomplete_and_uncertain_journal_records_stay_active(tmp_path):
    async def scenario():
        db, clock, svc, router, ex, sync = await account(tmp_path)
        record = JournalRecord(
            "pending",
            "requested",
            0,
            "amend",
            "unknown",
            json.dumps(
                {"before": {"trigger": "90"}, "after": {"trigger": "95"}, "fields": ["trigger"]}
            ),
            "{}",
            NOW.isoformat(),
        )
        async with db.unit_of_work() as u:
            store_record(u, record)
            compact_journal(u)
            assert len(u.recovery.active("journal:")) == 1
            store_record(u, replace(record, phase="result", result='{"code":"TIMEOUT"}'))
            compact_journal(u)
            assert len(u.recovery.active("journal:")) == 2
            u.recovery.put("outcome:pending", "UNKNOWN")
            compact_journal(u)
            assert len(u.recovery.active("journal:")) == 2
            u.recovery.put("outcome:pending", "NOT_APPLIED")
            compact_journal(u)
            assert not u.recovery.active("journal:")
            assert u.recovery.get(record.key) == record.encode()
            await u.commit()
        await sync.close()

    asyncio.run(scenario())


def test_d90_index_keeps_unresolved_pair_and_archived_replay_does_not_resurrect():
    async def scenario():
        db = InMemoryDatabase("a", Environment.BACKTEST)
        request = JournalRecord(
            "d90-op",
            "requested",
            0,
            "amend",
            "stop",
            json.dumps(
                {
                    "before": {"qty": "2", "cumulative": "0"},
                    "after": {"qty": "1"},
                    "fields": ["qty"],
                }
            ),
            "{}",
            NOW.isoformat(),
            action_key="trim",
        )
        result = replace(request, phase="result", result='{"code":"UNSUPPORTED"}')
        async with db.unit_of_work() as u:
            store_record(u, result)  # either arrival order must build the same index
            assert not u.recovery.active("d90:")
            store_record(u, request)
            assert u.recovery.active("d90:") == (("d90:d90-op", request.key),)
            u.recovery.put("outcome:d90-op", "NOT_APPLIED")
            u.recovery.put("replacement:trim", '{"done":false}')
            compact_journal(u)
            assert len(u.recovery.active("journal:")) == 2
            assert u.recovery.active("d90:")
            u.recovery.put("replacement:trim", '{"done":true}')
            compact_journal(u)
            assert not u.recovery.active("journal:")
            assert not u.recovery.active("d90:")
            assert not u.recovery.active("replacement:")
            await u.commit()
        async with db.unit_of_work() as u:
            store_record(u, request)
            store_record(u, result)
            assert not u.recovery.active("journal:")
            assert not u.recovery.active("d90:")
            assert u.recovery.get(request.key) == request.encode()

    asyncio.run(scenario())
