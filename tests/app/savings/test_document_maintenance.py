"""Forecast archive maintenance does not depend on settlement readiness or prevent shutdown."""

import asyncio
from threading import Event
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer

from open_energy_planner.savings import api as savings_api
from open_energy_planner.savings.storage.adapters.sqlalchemy import stamp
from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.savings.support.scenarios import START


def application(repository, *, activate=False):
    """Register the actual worker with missing optional setup and an optional recorded activation."""
    if activate:
        repository.state["plan"] = {
            "plan_id": "synthetic-maintenance",
            "generated_at": START.isoformat(),
            "steps": [],
        }
        repository.save()
        with repository.engine.begin() as connection:
            connection.execute(
                ACTIVATIONS.insert().values(
                    activated_at=stamp(START), plan_id="synthetic-maintenance"
                )
            )
    app = web.Application()
    _capture, runtime = savings_api.register(
        app,
        repository,
        SimpleNamespace(current=None),
        SimpleNamespace(current=None),
        SimpleNamespace(current=None),
        SimpleNamespace(configuration=None),
        SimpleNamespace(configuration=None),
        asyncio.Lock(),
        SimpleNamespace(value=None),
        lambda: None,
        mutation_lock=asyncio.Lock(),
    )
    return app, runtime


def test_document_index_progresses_while_settlement_prerequisites_are_missing(tmp_path):
    """A recorded activation without location still permits independent forecast reference backfill."""
    repository = SqlPlanRepository(tmp_path)

    async def scenario():
        """Observe real prerequisite failure and the independently committed maintenance cursor."""
        app, runtime = application(repository, activate=True)
        async with TestServer(app):
            async with asyncio.timeout(3):
                while not runtime.documents.exists("archive-retention.json"):
                    await asyncio.sleep(0.01)
            assert runtime.attention is True
            assert runtime.issue
            assert runtime.documents.read("archive-retention.json")["stage"] == "values"

    try:
        asyncio.run(scenario())
    finally:
        repository.close()


def test_shutdown_drains_failed_retention_and_stops_worker(tmp_path, monkeypatch):
    """Retention rollback after cancellation cannot restart the worker or hang application cleanup."""
    entered, release, drained = Event(), Event(), Event()
    repository = SqlPlanRepository(tmp_path)
    workers = []
    complete_write = savings_api.complete_write

    def failed_retention(_documents, _now):
        """Hold the worker's actual off-loop maintenance until shutdown is awaiting its rollback."""
        entered.set()
        try:
            assert release.wait(3)
            raise OSError("Synthetic archive rollback")
        finally:
            drained.set()

    monkeypatch.setattr(savings_api, "maintain_documents", failed_retention)

    async def observe_worker(operation, *arguments):
        """Capture the real maintenance owner before its off-loop writer starts."""
        if operation is failed_retention:
            workers.append(asyncio.current_task())
        return await complete_write(operation, *arguments)

    monkeypatch.setattr(savings_api, "complete_write", observe_worker)

    async def scenario():
        """Start actual lifecycle cleanup before releasing the interrupted maintenance operation."""
        app, _runtime = application(repository)
        server = TestServer(app)
        await server.start_server()
        cleanup = None
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            cleanup = asyncio.create_task(server.close())
            async with asyncio.timeout(2):
                while not workers[0].cancelling():
                    await asyncio.sleep(0)
            assert not drained.is_set()
            assert not cleanup.done()
            release.set()
            await asyncio.wait_for(cleanup, 2)
            assert drained.is_set()
        finally:
            release.set()
            if cleanup is None:
                await server.close()
            else:
                await asyncio.gather(cleanup, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        repository.close()
