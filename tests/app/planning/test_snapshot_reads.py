"""Keep appliance reads independent of historical settlement I/O under real HTTP."""

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from open_energy_planner.app import create_app
from open_energy_planner.build_info import BUILD_INFO
from open_energy_planner.savings.runtime import SavingsRuntime
from tests.app.planning.test_live_app import FutureSource, configure, running_app, send, wait_ready
from tests.ha_contract.support.metadata import setup_metadata


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


@pytest.mark.parametrize("failed", [False, True])
def test_appliance_settings_keep_published_state_until_consumer_write_finishes(
    tmp_path, monkeypatch, failed
):
    """Read both real settings routes while SQL is stalled, then verify commit or rollback."""
    from open_energy_planner.appliances.forecasting import CONSUMER_SETTINGS_POINTER
    from open_energy_planner.storage.adapters.documents import SqlDocuments

    async def scenario():
        """Deselect a measured device and verify its old and new projections stay coherent."""
        entity = "sensor.dishwasher_energy"
        metadata = setup_metadata()
        metadata["entities"].append(
            {
                "entity_id": entity,
                "name": "Dishwasher",
                "unit": "kWh",
                "device_id": "dishwasher",
                "device_class": "energy",
                "state_class": "total_increasing",
            }
        )
        metadata["suggestions"]["consumers"] = [{"entity_id": entity, "upstream": None}]
        entered, release = threading.Event(), threading.Event()
        original_write = SqlDocuments.write

        def stalled_write(documents, key, value):
            """Pause the real pointer write inside its transaction."""
            if key == CONSUMER_SETTINGS_POINTER and value == []:
                entered.set()
                assert release.wait(8)
                if failed:
                    raise OSError("Synthetic settings persistence failure")
            return original_write(documents, key, value)

        async with running_app(tmp_path) as (session, url):
            assert (await session.post(url + "/v1/ha/catalogue", json=metadata)).status == 200
            consumers_path = url + "/v1/consumers?view=settings"
            appliances_path = url + "/v1/appliances"
            original = await (await session.get(consumers_path)).json()
            selection = [{"entity_id": entity, "weather_sensitive": False}]
            selection_response = await session.put(
                consumers_path,
                json={"selected": selection, "expected_version": original["version"]},
            )
            assert selection_response.status == 200
            selected = await selection_response.json()
            configured = await session.post(
                appliances_path + "/device", json={"entity_id": entity, "shiftable": True}
            )
            assert configured.status == 200
            prior_appliances = (await configured.json())["appliances"]
            assert prior_appliances["devices"][0]["entity_id"] == entity
            monkeypatch.setattr(SqlDocuments, "write", stalled_write)
            save = asyncio.create_task(
                session.put(
                    consumers_path,
                    json={"selected": [], "expected_version": selected["version"]},
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                async with asyncio.timeout(1):
                    consumers, appliances, navigation = await asyncio.gather(
                        session.get(consumers_path),
                        session.get(appliances_path),
                        session.get(url + "/setup/session"),
                    )
                    assert await consumers.json() == selected
                    assert await appliances.json() == prior_appliances
                    assert navigation.status == 200
                assert not save.done()
            finally:
                release.set()
                saved = await save
            assert saved.status == (500 if failed else 200)
            consumers, appliances = await asyncio.gather(
                session.get(consumers_path), session.get(appliances_path)
            )
            assert (await consumers.json())["selected"] == (selection if failed else [])
            assert (await appliances.json())["devices"] == (
                prior_appliances["devices"] if failed else []
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["pause", "revoke"])
def test_snapshot_projection_does_not_block_and_discards_stale_controls(
    tmp_path, monkeypatch, action
):
    """Delay a live projection and require pause and revocation to each win before HTTP delivery."""
    from open_energy_planner.runtime.snapshot import SnapshotProjection

    async def scenario():
        """Allow ordinary reads and control persistence while projection is still suspended."""
        entered, release = threading.Event(), threading.Event()
        original_project = SnapshotProjection.project
        future = FutureSource()
        async with running_app(tmp_path, future) as (session, url):
            configured = await configure(session, url)
            await send(session, url, configured["measurement_config"])
            accepted = await wait_ready(session, url)
            assert accepted["enabled"] and accepted["plan"]

            def suspended_projection(projection):
                """Gate only the first real snapshot worker holding the old plan."""
                if not entered.is_set():
                    assert projection.plan["plan_id"] == accepted["plan"]["plan_id"]
                    entered.set()
                    assert release.wait(8)
                return original_project(projection)

            monkeypatch.setattr(SnapshotProjection, "project", suspended_projection)
            pending = asyncio.create_task(
                session.get(
                    url + "/v1/snapshot",
                    headers={
                        "X-OEP-Release": BUILD_INFO["version"],
                        "X-OEP-Build": BUILD_INFO["build"] or "",
                    },
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                async with asyncio.timeout(1):
                    read_paths = ("/v1/appliances", "/v1/consumers?view=settings", "/setup/session")
                    for response in await asyncio.gather(
                        *(session.get(url + path) for path in read_paths)
                    ):
                        assert response.status == 200
                        await response.read()
                    if action == "pause":
                        pause = await session.put(
                            url + "/v1/enabled",
                            json={"enabled": False, "request_id": "pause-projection"},
                        )
                        assert pause.status == 200
                    else:
                        future.available = False
                        revocation = await session.post(url + "/v1/replan")
                        assert revocation.status == 200
                        assert (await revocation.json())["plan"] is None
                assert not pending.done()
            finally:
                release.set()
                response = await pending
            assert response.status == 200
            assert {"snapshot_state_wait", "snapshot_document", "snapshot_json"} <= {
                part.strip().split(";")[0] for part in response.headers["Server-Timing"].split(",")
            }
            final = await response.json()
            if action == "pause":
                assert final["enabled"] is False
                assert final["plan"]["plan_id"] == accepted["plan"]["plan_id"]
                assert final["plan_invalidated"] is False
            else:
                assert final["enabled"] is True
                assert final["plan"] is None
                assert final["plan_invalidated"] is True

    asyncio.run(scenario())
