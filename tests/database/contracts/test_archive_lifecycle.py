"""Active archive damage, shared-engine ownership and local backup restoration."""

import json
from copy import deepcopy

import pytest
from sqlalchemy import create_engine, select

from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SCHEMA,
    STATE,
    SqlPlanRepository,
    archive_engine,
)
from tests.database.contracts.test_plan_archive_backends import plan
from tests.database.support.backends import BACKENDS

URLS = dict(BACKENDS)


@pytest.mark.parametrize("backend", [name for name, _ in BACKENDS])
@pytest.mark.parametrize("missing", ["metadata", "state"])
def test_active_archive_missing_singleton_is_not_reinitialized(tmp_path, backend, missing):
    """A damaged owned archive fails closed on every backend without replacing pairing or paused intent."""
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import META

    database_url = URLS[backend]
    engine = archive_engine(tmp_path, database_url)
    try:
        SCHEMA.drop_all(engine)
        original = SqlPlanRepository(tmp_path, database_url=database_url)
        try:
            assert original.owns_engine
            original.state["plan"] = plan()
            original.save()
            table = META if missing == "metadata" else STATE
            with original.engine.begin() as connection:
                connection.execute(table.delete())
        finally:
            original.close()
        with pytest.raises(OSError, match="missing"):
            SqlPlanRepository(tmp_path, database_url=database_url).close()
        with engine.connect() as connection:
            assert connection.execute(select(table)).first() is None
    finally:
        try:
            SCHEMA.drop_all(engine)
        finally:
            engine.dispose()


@pytest.mark.parametrize("backend", [name for name, _ in BACKENDS])
def test_price_lifecycle_borrows_the_only_active_database(tmp_path, backend):
    """Every app backend supplies the exact shared engine to price storage and retains it until all workers stop."""
    import asyncio
    from contextlib import asynccontextmanager

    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.app import create_app
    from open_energy_planner.prices.bootstrap import price_factory

    url = URLS[backend]
    engine = create_engine(url) if url else archive_engine(tmp_path)
    SCHEMA.drop_all(engine)

    @asynccontextmanager
    async def prices(active_engine):
        """Assert composition ownership while reusing production price composition without fetching providers."""
        assert active_engine is engine
        async with price_factory("NL")(active_engine) as service:
            assert service.selection.documents.engine is engine
            assert all(pair.store.engine is engine for pair in service.selection.comparisons)

            async def no_refresh(_now):
                """Keep the real composition test offline. Only engine ownership is under test."""
                return False

            service.refresh = no_refresh
            yield service

    async def scenario():
        """Run create_app with the injected price factory and its refresh method replaced by no_refresh."""
        async with TestClient(
            TestServer(
                create_app(
                    tmp_path,
                    "test-token-at-least-24-chars",
                    database_engine=engine,
                    price_factory=prices,
                    source="home_assistant",
                )
            )
        ) as client:
            assert (
                await client.get(
                    "/v1/prices", headers={"Authorization": "Bearer test-token-at-least-24-chars"}
                )
            ).status == 200

    try:
        asyncio.run(scenario())
        with engine.connect() as connection:
            assert connection.execute(select(STATE.c.id)).first()
    finally:
        SCHEMA.drop_all(engine)
        engine.dispose()


def test_sqlite_restore_in_another_data_directory_preserves_identity(tmp_path, monkeypatch):
    """Relative/absolute paths and restored volume locations refer to the same local archive identity, without migration."""
    import shutil

    monkeypatch.chdir(tmp_path)
    original = SqlPlanRepository("original")
    original.state["plan"] = plan()
    original.state["enabled"] = False
    original.save()
    expected = deepcopy(original.state)
    original.close()
    reopened = SqlPlanRepository(tmp_path / "original")
    assert reopened.state == expected
    reopened.close()
    shutil.copytree(tmp_path / "original", tmp_path / "restored")
    restored = SqlPlanRepository(tmp_path / "restored")
    try:
        assert restored.state == expected and restored.get_plan("plan-a") == plan()
        assert (
            json.loads((tmp_path / "restored" / "archive-backend.json").read_text())["location"][
                "database"
            ]
            == "archive.sqlite3"
        )
    finally:
        restored.close()
