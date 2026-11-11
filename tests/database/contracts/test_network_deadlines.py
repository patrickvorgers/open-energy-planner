"""Prove owned engines stop waiting when a live database cannot deliver responses."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from open_energy_planner.storage.adapters.lifecycle import sqlalchemy as archive_adapter
from tests.database.support.backends import BACKENDS
from tests.database.support.network import paused_responses

NETWORK_BACKENDS = [(name, url) for name, url in BACKENDS if name != "sqlite"]


@pytest.mark.parametrize(
    "backend,url", NETWORK_BACKENDS, ids=[name for name, _ in NETWORK_BACKENDS]
)
def test_cancelled_settlement_reader_drains_and_releases_mutation_lock(
    tmp_path, monkeypatch, backend, url
):
    """A cancelled real settlement handler finishes its stalled SQL worker before releasing ownership."""
    import asyncio
    from threading import Event
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from open_energy_planner.savings.api import register
    from open_energy_planner.savings.runtime import SavingsRuntime

    monkeypatch.setattr(archive_adapter, "DATABASE_IO_TIMEOUT_SECONDS", 0.3)

    async def scenario(proxied, paused, received):
        """Repeated cancellation keeps the worker owned until its native I/O deadline closes the socket."""
        repository = archive_adapter.SqlPlanRepository(tmp_path, database_url=proxied)
        application = web.Application()
        mutation_lock = asyncio.Lock()
        register(
            application,
            repository,
            SimpleNamespace(current=None),
            SimpleNamespace(current=None),
            SimpleNamespace(current=None),
            SimpleNamespace(configuration=None),
            SimpleNamespace(configuration=None),
            asyncio.Lock(),
            SimpleNamespace(value=None),
            lambda: None,
            mutation_lock=mutation_lock,
        )
        acquired, proceed, drained = Event(), Event(), Event()

        def request(runtime, *_arguments):
            """Run real SQL through the app-owned engine and expose completion independently of the handler."""
            try:
                with runtime.store.engine.connect() as connection:
                    acquired.set()
                    assert proceed.wait(3)
                    return connection.scalar(text("SELECT 1"))
            finally:
                drained.set()

        monkeypatch.setattr(SavingsRuntime, "request", request)
        handler = next(
            route.handler
            for route in application.router.routes()
            if route.method == "POST" and route.resource.canonical == "/v1/settlement"
        )
        incoming = make_mocked_request("POST", "/v1/settlement", app=application)
        incoming.json = AsyncMock(return_value={})
        pending = asyncio.create_task(handler(incoming))
        try:
            assert await asyncio.to_thread(acquired.wait, 1)
            paused.set()
            proceed.set()
            assert await asyncio.to_thread(received.wait, 1)
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            await asyncio.sleep(0)
            assert mutation_lock.locked() and not pending.done()
            completed, _ = await asyncio.wait((pending,), timeout=2)
            assert pending in completed
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert drained.is_set()
            assert not mutation_lock.locked()
        finally:
            paused.clear()
            proceed.set()
            await asyncio.gather(pending, return_exceptions=True)
            repository.close()

    with paused_responses(url) as arguments:
        asyncio.run(scenario(*arguments))


@pytest.mark.parametrize("backend,url", BACKENDS, ids=[name for name, _ in BACKENDS])
def test_stalled_database_response_is_bounded_and_connection_can_recover(
    tmp_path, monkeypatch, backend, url
):
    """A real query times out on withheld network bytes and a new connection remains usable."""
    monkeypatch.setattr(archive_adapter, "DATABASE_IO_TIMEOUT_SECONDS", 0.1)
    if backend == "sqlite":
        engine = archive_adapter.archive_engine(tmp_path)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("PRAGMA busy_timeout")) == 5000
        finally:
            engine.dispose()
        return
    with paused_responses(url) as (proxied, paused, received):
        engine = archive_adapter.archive_engine(tmp_path, proxied)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT 1")) == 1
                with ThreadPoolExecutor(max_workers=1) as readers:
                    paused.set()
                    pending = readers.submit(connection.scalar, text("SELECT 1"))
                    try:
                        assert received.wait(1)
                        with pytest.raises(DBAPIError):
                            pending.result(timeout=2)
                        assert connection.invalidated
                    finally:
                        paused.clear()
                        with suppress(DBAPIError):
                            pending.result(timeout=3)
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT 1")) == 1
        finally:
            engine.dispose()
