"""Shared test root for V0 and module-level fixtures."""

from datetime import UTC, datetime

import pytest


def pytest_generate_tests(metafunc):
    if metafunc.module.__name__ in {
        "test_oms",
        "test_risk",
        "test_portfolio",
        "test_recovery_integration",
        "test_trailing_stop",
    }:
        metafunc.parametrize("storage_contract_backend", ["memory", "sqlite"], indirect=True)


@pytest.fixture(autouse=True)
def storage_contract_backend(request, monkeypatch, tmp_path):
    """Run existing OMS/risk/portfolio acceptance cases against both storage adapters."""
    module = request.module
    if module.__name__ not in {
        "test_oms",
        "test_risk",
        "test_portfolio",
        "test_recovery_integration",
        "test_trailing_stop",
    }:
        return
    if request.param == "memory":
        return
    from richletsgo.storage.sqlite import SqliteDatabase, SqliteUnitOfWork

    class ReadyUnitOfWork(SqliteUnitOfWork):
        async def __aenter__(self):
            if not self.database.ready:
                await self.database.migrate()
                self.database.ready = True
            return await super().__aenter__()

    class Database(SqliteDatabase):
        ready = False

        def unit_of_work(self):
            return ReadyUnitOfWork(self)

    serial = 0

    def factory(account_id, environment):
        nonlocal serial
        serial += 1
        return Database(
            tmp_path / f"account-{serial}.sqlite",
            account_id,
            environment,
            now=lambda: datetime(2026, 10, 20, tzinfo=UTC),
        )

    import sys

    for name in ("test_portfolio", "test_recovery_integration"):
        imported = sys.modules.get(name)
        if imported is not None and hasattr(imported, "InMemoryDatabase"):
            monkeypatch.setattr(imported, "InMemoryDatabase", factory)
    if hasattr(module, "InMemoryDatabase"):
        monkeypatch.setattr(module, "InMemoryDatabase", factory)
    if hasattr(module, "InMemoryUnitOfWork"):
        monkeypatch.setattr(module, "InMemoryUnitOfWork", ReadyUnitOfWork)
