"""Keep appliance reads independent of historical settlement I/O under real HTTP."""

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from open_energy_planner.app import create_app
from open_energy_planner.savings.runtime import SavingsRuntime
from tests.app.planning.test_live_app import running_app


@pytest.mark.parametrize("operation", ["snapshot", "settlement"])
def test_settlement_history_read_does_not_hold_appliance_state_lock(
    tmp_path, monkeypatch, operation
):
    """A gated historical lookup must not delay authenticated appliance configuration reads."""

    async def scenario():
        """Block only the historical reader and release it even if the independent request fails."""
        entered, release = threading.Event(), threading.Event()
        readers, readers_lock = [], threading.Lock()
        async with running_app(tmp_path) as (session, url):

            def request(_runtime, *_arguments):
                """Simulate a slow archive without any production data or timing assumptions."""
                with readers_lock:
                    readers.append(True)
                    if len(readers) == 2:
                        entered.set()
                assert release.wait(3)

            monkeypatch.setattr(SavingsRuntime, "request", request)
            first = asyncio.create_task(
                session.get(url + "/v1/snapshot")
                if operation == "snapshot"
                else session.post(url + "/v1/settlement", json={})
            )
            second = asyncio.create_task(session.get(url + "/v1/snapshot"))
            pending = (first, second)
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                async with asyncio.timeout(1):
                    async with session.put(
                        url + "/v1/enabled",
                        json={"enabled": False, "request_id": "pause-during-read"},
                    ) as response:
                        assert response.status == 200
                        assert await response.json() == {
                            "accepted": True,
                            "enabled": False,
                            "request_id": "pause-during-read",
                        }
                assert not release.is_set() and all(not reader.done() for reader in pending)
                assert len(readers) == 2
                response = await asyncio.wait_for(session.get(url + "/v1/appliances"), 0.5)
                assert response.status == 200
                assert (await response.json())["devices"] == []
            finally:
                release.set()
                response, snapshot = await asyncio.gather(*pending)
            assert snapshot.status == 200
            full = await snapshot.json()
            assert full["enabled"] is False
            assert "settlement_request" in full and "consumers" in full and "installation" in full
            if operation == "snapshot":
                assert response.status == 200
                assert (await response.json())["enabled"] is False
            else:
                assert response.status == 409
                assert await response.text() == "No settlement capture is pending"

    asyncio.run(scenario())


def test_failed_control_persistence_never_returns_an_acceptance(tmp_path, monkeypatch):
    """A failed intent write returns an error rather than acknowledging uncommitted state."""
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

    async def scenario():
        """Fail only the explicit control write and inspect the independent durable state afterward."""
        async with running_app(tmp_path) as (session, url):
            original = SqlPlanRepository.save

            def fail_control(repository):
                """Leave setup and shutdown writes intact while refusing this control transaction."""
                if repository.state["last_control_id"] == "failed-pause":
                    raise OSError("Synthetic control persistence failure")
                return original(repository)

            with monkeypatch.context() as patch:
                patch.setattr(SqlPlanRepository, "save", fail_control)
                async with session.put(
                    url + "/v1/enabled",
                    json={"enabled": False, "request_id": "failed-pause"},
                ) as response:
                    assert response.status == 500
                stored = SqlPlanRepository(tmp_path)
                try:
                    assert stored.state["enabled"] is True
                    assert stored.state["last_control_id"] != "failed-pause"
                finally:
                    stored.close()

    asyncio.run(scenario())


def test_settlement_source_change_during_read_rejects_ingress(tmp_path, monkeypatch):
    """A source revision changed during off-loop validation cannot authorize historical writes."""
    from types import SimpleNamespace

    from open_energy_planner.savings.api import register
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

    async def scenario():
        """Use the real route while holding its reader at the asynchronous ownership boundary."""
        entered, release = threading.Event(), threading.Event()
        original_source, replacement_source = object(), object()
        installation = SimpleNamespace(current=original_source)
        repository = SqlPlanRepository(tmp_path)
        application = web.Application()
        register(
            application,
            repository,
            SimpleNamespace(current=None),
            SimpleNamespace(current=None),
            installation,
            SimpleNamespace(configuration=None),
            SimpleNamespace(configuration=None),
            asyncio.Lock(),
            SimpleNamespace(value=None),
            lambda: None,
            mutation_lock=asyncio.Lock(),
        )

        def request(_runtime, _now, source, *_arguments):
            """Confirm the worker borrows the captured source rather than reading mutable owners."""
            assert source is original_source
            entered.set()
            assert release.wait(3)
            return {"version": "captured-request"}

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
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                installation.current = replacement_source
            finally:
                release.set()
            with pytest.raises(web.HTTPConflict) as conflict:
                await pending
            assert conflict.value.text == "Settlement request changed"
        finally:
            repository.close()

    asyncio.run(scenario())


def test_cancelled_snapshot_drains_historical_reader_before_handler_finishes(tmp_path, monkeypatch):
    """Repeated cancellation cannot leave an archive reader using an engine after request cleanup."""

    async def scenario():
        """Invoke the actual snapshot handler so HTTP client disconnect policy cannot hide cancellation."""
        entered, release = threading.Event(), threading.Event()
        application = create_app(
            tmp_path, "read-cancellation-test-0123456789abcdef", source="home_assistant"
        )
        async with TestClient(TestServer(application)):

            def request(_runtime, *_arguments):
                """Borrow the archive engine until the test permits the worker to finish."""
                entered.set()
                assert release.wait(3)

            monkeypatch.setattr(SavingsRuntime, "request", request)
            handler = next(
                route.handler
                for route in application.router.routes()
                if route.method == "GET" and route.resource.canonical == "/v1/snapshot"
            )
            pending = asyncio.create_task(
                handler(make_mocked_request("GET", "/v1/snapshot", app=application))
            )
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                pending.cancel()
                await asyncio.sleep(0)
                pending.cancel()
                await asyncio.sleep(0)
                assert not pending.done()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending

    asyncio.run(scenario())
