"""File-serialized SQLite UoW. Connections never leave their dedicated worker thread."""

import asyncio
import json
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TypeVar
from uuid import uuid4

from richletsgo.domain import Environment
from richletsgo.portfolio.models import CloseState
from richletsgo.risk.records import EpisodeState

from .codec import decode, encode, milliseconds
from .contracts import FillStatus
from .errors import (
    CommitOutcomeUnknown,
    DuplicateExecId,
    DuplicateSignalId,
    StorageBusy,
    StorageError,
)
from .memory import InMemoryDatabase, InMemoryUnitOfWork, _State
from .paths import local_database_path
from .schema import TABLES, VERSION
from .working_set import signal_time, working_set

T = TypeVar("T")


@dataclass
class _File:
    executor: ThreadPoolExecutor
    lock: asyncio.Lock
    unresolved: dict[str, str]
    resolved: dict[str, bool]


_FILES: dict[Path, _File] = {}
_REGISTRY_LOCK = threading.Lock()


def open_readonly(path: Path) -> sqlite3.Connection:
    """Independent viewer connection; the caller owns and closes it on its own thread."""
    connection = sqlite3.connect(local_database_path(path).as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


async def _drain[T](future: asyncio.Future[T]) -> T:
    # A second cancel must not strand a worker holding a connection or a write lock.
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
    return future.result()


class SqliteDatabase(InMemoryDatabase):
    now: Callable[[], datetime]

    def __init__(
        self,
        path: Path,
        account_id: str,
        environment: Environment,
        *,
        working_set_days: int = 7,
        busy_timeout_seconds: float = 5,
        outcome_timeout_seconds: float = 10,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        fault: Callable[[str, sqlite3.Connection], None] | None = None,
        alert: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(account_id, environment)
        if working_set_days < 7 or min(busy_timeout_seconds, outcome_timeout_seconds) <= 0:
            raise ValueError("invalid storage retention or timeout")
        self.path = local_database_path(path)
        self.working_set_days = working_set_days
        self.busy_timeout_seconds = busy_timeout_seconds
        self.outcome_timeout_seconds = outcome_timeout_seconds
        self.now, self.fault, self.alert = now, fault, alert
        with _REGISTRY_LOCK:
            self._file = _FILES.setdefault(
                self.path,
                _File(
                    ThreadPoolExecutor(max_workers=1, thread_name_prefix="rlg-sqlite"),
                    asyncio.Lock(),
                    {},
                    {},
                ),
            )
        self._lock = self._file.lock

    def unit_of_work(self) -> "SqliteUnitOfWork":
        return SqliteUnitOfWork(self)

    @property
    def entry_paused(self) -> bool:
        return self.account_id in self._file.unresolved.values()

    def _submit(self, callback: Callable[[], T]) -> asyncio.Future[T]:
        return asyncio.get_running_loop().run_in_executor(self._file.executor, callback)

    async def _exclusive(self, callback: Callable[[], T]) -> T:
        async with self._lock:
            future = self._submit(callback)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                await _drain(future)
                raise

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=self.busy_timeout_seconds, isolation_level=None
        )
        try:
            connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_seconds * 1000)}")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except BaseException:
            connection.close()
            raise

    def _verify(self, connection: sqlite3.Connection) -> None:
        versions = connection.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        if versions != [(VERSION,)]:
            raise StorageError("database schema version mismatch; run migrations explicitly")
        row = connection.execute("SELECT value FROM meta WHERE key='environment'").fetchone()
        if row != (self.environment.value,):
            raise StorageError("database environment mismatch")

    async def migrate(self) -> None:
        def run() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='schema_version'"
                ).fetchone()
                if not exists:
                    script = (
                        files("richletsgo.storage")
                        .joinpath("migrations/V001__init.sql")
                        .read_text(encoding="utf-8")
                    )
                    # executescript implicitly commits: execute statements individually instead.
                    for statement in script.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_version VALUES (?,?)",
                        (VERSION, milliseconds(self.now())),
                    )
                    connection.execute(
                        "INSERT INTO meta VALUES ('environment',?)", (self.environment.value,)
                    )
                    connection.execute(
                        "INSERT INTO meta VALUES ('account_id',?)", (self.account_id,)
                    )
                self._verify(connection)
                connection.commit()
            finally:
                connection.close()

        await self._exclusive(run)

    def _unknown(self, txn_id: str) -> None:
        self._file.unresolved[txn_id] = self.account_id
        if self.alert:
            self.alert("ENTRY_PAUSED_SYSTEM(storage): " + txn_id)

    async def resolve(self, txn_id: str) -> bool:
        """Determine the result only. Resume requires journal reconciliation by the caller."""
        if self._file.unresolved.get(txn_id) != self.account_id:
            raise ValueError("transaction is not awaiting resolution for this account")

        def run() -> bool:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._verify(connection)
                return (
                    connection.execute(
                        "SELECT 1 FROM uow_commits WHERE txn_id=? AND account_id=?",
                        (txn_id, self.account_id),
                    ).fetchone()
                    is not None
                )
            finally:
                connection.close()

        try:
            async with asyncio.timeout(self.outcome_timeout_seconds):
                result = await self._exclusive(run)
                self._file.resolved[txn_id] = result
                return result
        except (TimeoutError, sqlite3.Error):
            if self.alert:
                self.alert("storage outcome resolution failed: " + txn_id)
            raise

    def reconciled(self, txn_id: str) -> None:
        """Only after journal merge, outbox acknowledgement and order reconciliation."""
        if txn_id not in self._file.resolved:
            raise ValueError("resolve the transaction before completing reconciliation")
        self._file.unresolved.pop(txn_id, None)
        self._file.resolved.pop(txn_id, None)

    async def recover(self, reconcile: Callable[[], Awaitable[bool]]) -> bool:
        """Resolve, then run Synchronizer.synchronize (journal first), then release entry pause."""
        pending = tuple(
            k for k, account in self._file.unresolved.items() if account == self.account_id
        )
        for txn_id in pending:
            await self.resolve(txn_id)
        if not await reconcile():
            return False
        for txn_id in pending:
            self.reconciled(txn_id)
        return not self.entry_paused

    async def backup(self, directory: Path) -> Path:
        directory = local_database_path(directory / "probe.sqlite").parent
        at = self.now()
        target = directory / f"{self.environment.value}-{at:%Y-%m-%d}.sqlite"
        if target == self.path:
            raise ValueError("backup cannot overwrite the source")

        def run() -> Path:
            directory.mkdir(parents=True, exist_ok=True)
            source = self._connect()
            destination = sqlite3.connect(target)
            try:
                self._verify(source)
                source.backup(destination)
                result = destination.execute("PRAGMA integrity_check").fetchall()
                if result != [("ok",)]:
                    raise StorageError("backup integrity check failed")
            finally:
                destination.close()
                source.close()
            target.with_suffix(".integrity.txt").write_text("ok\n", encoding="utf-8")
            for old in directory.glob(f"{self.environment.value}-????-??-??.sqlite"):
                try:
                    date = datetime.strptime(
                        old.stem.removeprefix(self.environment.value + "-"), "%Y-%m-%d"
                    ).replace(tzinfo=UTC)
                except ValueError:
                    continue
                if date.date() < (at - timedelta(days=14)).date():
                    old.unlink()
                    old.with_suffix(".integrity.txt").unlink(missing_ok=True)
            return target

        return await self._exclusive(run)


def _rows(state: _State, field: str) -> dict[str, str]:
    value = getattr(state, field)
    if isinstance(value, dict):
        return {encode(k): encode(v) for k, v in value.items()}
    if isinstance(value, set):
        return {encode(k): encode(True) for k in value}
    return {encode(i): encode(v) for i, v in enumerate(value)}


class SqliteUnitOfWork(InMemoryUnitOfWork):
    database: SqliteDatabase

    def __init__(self, database: SqliteDatabase) -> None:
        super().__init__(database)
        self.txn_id = uuid4().hex
        self._connection: sqlite3.Connection | None = None
        self._baseline = _State()

    @property
    def entry_paused(self) -> bool:
        return self.database.entry_paused

    def _hook(self, stage: str) -> None:
        if self.database.fault:
            assert self._connection is not None
            self.database.fault(stage, self._connection)

    def _open_load(self) -> None:
        self._connection = self.database._connect()
        self._hook("before_begin")
        self._connection.execute("BEGIN IMMEDIATE")
        self.database._verify(self._connection)
        self._hook("before_load")
        state = _State()
        cutoff = self.database.now() - timedelta(days=self.database.working_set_days)
        # Load dependencies first, then indexed order rows plus exact referenced identities.
        ordered = [f for f in TABLES if f != "orders"] + ["orders"]
        for field in ordered:
            table = TABLES[field]
            container = getattr(state, field)
            condition = "account_id=?"
            parameters: tuple[Any, ...] = (self.database.account_id,)
            if field in ("fills", "decisions", "intents", "episodes", "orders"):
                condition += " AND (active=1 OR updated_at>=?)"
                parameters += (milliseconds(cutoff),)
            for key, payload in self._connection.execute(
                f"SELECT key,payload FROM {table} WHERE {condition} ORDER BY rowid",
                parameters,
            ):
                k, value = decode(key), decode(payload)
                if isinstance(container, dict):
                    container[k] = value
                elif isinstance(container, set):
                    container.add(k)
                else:
                    container.append(value)
        referenced_episodes = {i.episode_id for i in state.intents.values()}
        for key in referenced_episodes - state.episodes.keys():
            row = self._connection.execute(
                "SELECT payload FROM episodes WHERE account_id=? AND key=?",
                (self.database.account_id, encode(key)),
            ).fetchone()
            if row:
                state.episodes[key] = decode(row[0])
        references = {k for e in state.episodes.values() for k in e.entry_order_keys}
        references.update(s.order_key for b in state.books.values() for s in b.stops)
        references.update(a.order_key for a in state.actions.values() if a.order_key)
        references.update(f.order_key for f in state.fills.values() if f.order_key)
        for key in references - state.orders.keys():
            row = self._connection.execute(
                "SELECT payload FROM orders WHERE account_id=? AND key=?",
                (self.database.account_id, encode(key)),
            ).fetchone()
            if row:
                state.orders[key] = decode(row[0])
        for key in state.recovery_data:
            prefix = key.partition(":")[0] + ":"
            state.recovery_index.setdefault(prefix, {})[key] = None
        self._working = working_set(state, cutoff)
        self._baseline = self.state.clone()
        # Unlike memory, the database object is not this transaction's archive owner.
        # Keep the comparison snapshot independent when repositories archive records.
        self._baseline.recovery_archive = self.state.recovery_archive.copy()

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError("nested/reentrant unit of work is not supported")
        await self.database._lock.acquire()
        self._entered = True
        future = self.database._submit(self._open_load)
        try:
            await asyncio.shield(future)
        except BaseException as error:
            try:
                await _drain(future)
            except BaseException:
                pass
            await self._close()
            self._release()
            if isinstance(error, sqlite3.OperationalError) and "locked" in str(error):
                raise StorageBusy("SQLite write lock timed out") from error
            if isinstance(error, sqlite3.Error):
                raise StorageError("SQLite transaction initialization failed") from error
            raise
        return self

    def _flush(self) -> None:
        assert self._connection is not None
        connection = self._connection
        now = milliseconds(self.database.now())
        self._hook("before_flush")
        assert self._baseline is not None
        for field, table in TABLES.items():
            before, after = _rows(self._baseline, field), _rows(self.state, field)
            for key in before.keys() - after.keys():
                connection.execute(
                    f"DELETE FROM {table} WHERE account_id=? AND key=?",
                    (self.database.account_id, key),
                )
            for key, payload in after.items():
                if before.get(key) == payload:
                    continue
                if key not in before and field in ("fills", "decisions"):
                    exists = connection.execute(
                        f"SELECT 1 FROM {table} WHERE account_id=? AND key=?",
                        (self.database.account_id, key),
                    ).fetchone()
                    if exists:
                        raise (DuplicateExecId if field == "fills" else DuplicateSignalId)(
                            "duplicate persistent identity"
                        )
                columns = "account_id,key,payload,updated_at,active"
                value = decode(payload)
                at, active = now, 1
                if field == "orders":
                    at = milliseconds(value.updated_at)
                    active = int(not value.state.is_terminal or bool(value.unbooked_qty))
                elif field == "fills":
                    at = milliseconds(value.event.exec_time)
                    active = int(value.status is not FillStatus.BOOKED)
                elif field == "decisions":
                    at = milliseconds(signal_time(value.signal_id) or value.decided_at)
                    active = 0
                elif field == "episodes":
                    at = milliseconds(value.opened_at)
                    active = int(value.state is not EpisodeState.CLOSED)
                elif field == "intents":
                    at = milliseconds(value.created_at)
                    active = int(value.state is not CloseState.DONE)
                values: tuple[Any, ...] = (self.database.account_id, key, payload, at, active)
                if field == "episodes":
                    episode = decode(payload)
                    columns += ",strategy,symbol,closed_at"
                    values += (
                        episode.strategy.value,
                        episode.symbol.value,
                        now if episode.state is EpisodeState.CLOSED else None,
                    )
                updates = ",".join(f"{c}=excluded.{c}" for c in columns.split(",")[2:])
                connection.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in values)}) "
                    f"ON CONFLICT(account_id,key) DO UPDATE SET {updates}",
                    values,
                )
                if field in ("recovery_data", "recovery_archive"):
                    self._project_recovery(decode(key), value, now)
        connection.execute(
            "INSERT INTO uow_commits VALUES (?,?,?)", (self.txn_id, self.database.account_id, now)
        )
        cutoff = now - self.database.working_set_days * 86_400_000
        for (old,) in connection.execute(
            "SELECT txn_id FROM uow_commits WHERE committed_at<?", (cutoff,)
        ).fetchall():
            if old not in self.database._file.unresolved:
                connection.execute("DELETE FROM uow_commits WHERE txn_id=?", (old,))
        self._hook("before_commit")

    def _project_recovery(self, key: str, value: str, now: int) -> None:
        """Typed audit projections share the transaction with the existing M1 recovery API."""
        assert self._connection is not None
        c, account = self._connection, self.database.account_id
        if key.startswith("journal:"):
            from richletsgo.oms.journal import JournalRecord

            record = JournalRecord.decode(value)
            c.execute(
                "INSERT INTO protective_operations VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id,operation_id,phase) DO UPDATE SET payload=excluded.payload",
                (
                    account,
                    record.operation_id,
                    record.phase,
                    record.group_id,
                    str(record.step) if record.step is not None else None,
                    record.attempt,
                    record.action_key,
                    value,
                    milliseconds(datetime.fromisoformat(record.ts)),
                ),
            )
        elif key in ("pauses", "equity_tracker") or key.startswith("resume_record:"):
            c.execute(
                "INSERT INTO risk_states VALUES (?,?,?,?) ON CONFLICT(account_id,key) "
                "DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at",
                (account, key, value, now),
            )
        elif key.startswith("command:") and value.startswith("{"):
            record = json.loads(value)
            c.execute(
                "INSERT INTO commands VALUES (?,?,?,?,?,?) ON CONFLICT(account_id,request_id) "
                "DO UPDATE SET state=excluded.state,payload=excluded.payload",
                (account, record["request_id"], record["source"], record["state"], value, now),
            )
            for target in record.get("targets", []):
                c.execute(
                    "INSERT INTO command_targets VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(account_id,request_id,order_key) DO UPDATE SET "
                    "state=excluded.state,attempts=excluded.attempts,payload=excluded.payload",
                    (
                        account,
                        record["request_id"],
                        target["order_key"],
                        target["state"],
                        target["attempts"],
                        json.dumps(target, ensure_ascii=False),
                    ),
                )

    def _commit(self) -> None:
        assert self._connection is not None
        self._hook("commit")
        self._connection.commit()
        self._hook("after_commit")

    async def commit(self) -> None:
        _ = self.state  # reject double commit
        future = self.database._submit(self._flush)
        try:
            await asyncio.shield(future)
        except BaseException:
            try:
                await _drain(future)
            finally:
                await self._close()
            raise
        future = self.database._submit(self._commit)
        try:
            await asyncio.shield(future)
        except BaseException as error:
            self.database._unknown(self.txn_id)
            try:
                await _drain(future)
            except BaseException:
                pass
            await self._close()
            raise CommitOutcomeUnknown(self.txn_id) from error
        await self._close()

    def _close_worker(self) -> None:
        if self._connection is not None:
            try:
                self._connection.rollback()
            finally:
                self._connection.close()
                self._connection = None

    async def _close(self) -> None:
        await _drain(self.database._submit(self._close_worker))
        self._working = None

    async def rollback(self) -> None:
        await self._close()

    def _release(self) -> None:
        self._entered = False
        self.database._lock.release()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            await self._close()
        finally:
            self._release()
