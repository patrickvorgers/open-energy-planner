"""Keep acknowledged panel metadata fresh independently of unavailable command snapshots."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from aiohttp import (
    ClientConnectionError,
    ClientResponseError,
    ClientSession,
    ServerFingerprintMismatch,
)
from homeassistant.util import dt as dt_util

from custom_components.open_energy_planner.client import AuthenticationError, ReleaseMismatchError
from tests.custom_component.support.chain import TOKEN, running_chain

HTTP_GATE_TIMEOUT_SECONDS = 10


@asynccontextmanager
async def _accepted_metadata(*, measurement=False, fixture_devices=True):
    """Own real HA watches and an authenticated app with a previously acknowledged catalogue."""
    async with running_chain(live=True, fixture_devices=fixture_devices) as (
        hass,
        entry,
        url,
        _root,
    ):
        coordinator = entry.runtime_data
        coordinator.poll_cancel()
        coordinator.poll_cancel = None
        if measurement:
            hass.states.async_set("sensor.diagnostic_grid", "0", {"unit_of_measurement": "W"})
            await coordinator.client.request(
                "PUT",
                "/v1/sources",
                {
                    "bindings": [
                        {
                            "key": "grid.power",
                            "entity_id": "sensor.diagnostic_grid",
                            "quantity": "power",
                        }
                    ],
                    "expected_revision": None,
                },
            )
        await coordinator.sync()
        assert coordinator.catalogue_discovery.healthy
        async with ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session:

            async def status(*, metadata=False):
                """Read provenance without implicitly requesting a metadata refresh."""
                path = "/v1/setup" if metadata else "/v1/ha/catalogue/refresh"
                async with session.get(url + path) as response:
                    assert response.status == 200
                    data = await response.json()
                    return data["catalogue"] if metadata else data

            yield hass, coordinator, status


@pytest.mark.parametrize("failure_path", ["/v1/snapshot", "/v1/observations"])
@pytest.mark.parametrize("failure_type", [TimeoutError, ClientConnectionError])
@pytest.mark.parametrize("measurement", [False, True])
def test_healthy_panel_metadata_confirmation_survives_command_snapshot_timeout(
    monkeypatch, failure_path, failure_type, measurement
):
    """A failed command snapshot must not prevent confirming unchanged watched source metadata."""

    async def scenario():
        """Keep real HA watches and authenticated app confirmation while one command read times out."""
        async with _accepted_metadata(measurement=measurement) as (_hass, coordinator, status):
            original_request = coordinator.client.request
            confirmations = []
            if measurement:
                accepted = await status(metadata=True)
                assert "sensor.diagnostic_grid" in {
                    entity["entity_id"] for entity in accepted["data"]["entities"]
                }
                snapshot = await original_request("GET", "/v1/snapshot")
                configuration = snapshot["measurement_config"]
                assert configuration["bindings"][0]["entity_id"] == "sensor.diagnostic_grid"

            async def request(method, path, body=None):
                """Fail only the command snapshot, retaining every other actual app HTTP request."""
                if path == failure_path:
                    raise failure_type("Synthetic command transport failure")
                if path == "/v1/ha/catalogue" and "confirmed_revision" in (body or {}):
                    confirmations.append(body)
                data = await original_request(method, path, body)
                if (
                    not measurement
                    and path == "/v1/snapshot"
                    and failure_path == "/v1/observations"
                ):
                    data["measurement_config"] = {
                        "revision": "diagnostic-grid",
                        "bindings": [
                            {
                                "key": "grid.power",
                                "entity_id": "sensor.diagnostic_grid",
                                "quantity": "power",
                                "required": True,
                                "multiplier": 1,
                                "maximum_age_s": 300,
                                "timestamp_attribute": None,
                            }
                        ],
                    }
                    data["config_revision"] = "diagnostic-grid"
                return data

            before = await status()
            coordinator.catalogue_confirmed_at = dt_util.utcnow() - timedelta(minutes=2)
            monkeypatch.setattr(coordinator.client, "request", request)
            await coordinator.sync()
            after = await status()
            assert coordinator.issue == "synchronization_failed"
            assert coordinator.catalogue_discovery.healthy
            assert confirmations == [{"confirmed_revision": coordinator.catalogue_context.revision}]
            assert after["revision"] == before["revision"]
            assert after["published_at"] == before["published_at"]
            assert after["confirmed_at"] > before["confirmed_at"]
            await coordinator.sync()
            assert len(confirmations) == 1
            assert await status() == after
            monkeypatch.setattr(coordinator.client, "request", original_request)
            await coordinator.sync()
            assert coordinator.issue != "synchronization_failed"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "caller", ["normal", "recovery", "confirmation_empty", "confirmation_newer"]
)
def test_changed_publication_preserves_real_configuration_rejection(monkeypatch, caller):
    """An accepted app configuration rejection must revoke old plans despite a concurrent watch change."""
    import json
    from copy import deepcopy

    from homeassistant.components.energy.data import async_get_manager

    from custom_components.open_energy_planner.protocol import RevokedPlan
    from open_energy_planner.home_assistant.setup_catalogue import SetupDiscovery
    from tests.ha_contract.support.commands import command_plan

    async def scenario():
        """Retain a deliberately stale command reply while the real app rejects incomplete Energy setup."""
        entered, release = asyncio.Event(), asyncio.Event()
        original_discovery = SetupDiscovery.discovery
        active = False
        responses = []

        async def discovery(owner, request):
            """Retain the actual accepted response and deliver a real HA event before returning it."""
            response = await original_discovery(owner, request)
            if active:
                data = json.loads(response.body)
                if "configuration_issue" in data:
                    responses.append(data)
                if not entered.is_set():
                    entered.set()
                    await release.wait()
                else:
                    hass.states.async_set(
                        "sensor.diagnostic_grid", "0", {"unit_of_measurement": "MW"}
                    )
                    await hass.async_block_till_done()
            return response

        monkeypatch.setattr(SetupDiscovery, "discovery", discovery)
        async with _accepted_metadata(measurement=True, fixture_devices=False) as (
            hass,
            coordinator,
            status,
        ):
            for identity in ("sensor.import_energy", "sensor.export_energy", "sensor.roof_energy"):
                hass.states.async_set(
                    identity,
                    "0",
                    {
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "total_increasing",
                        "friendly_name": "Original roof"
                        if identity == "sensor.roof_energy"
                        else identity,
                    },
                )
            manager = await async_get_manager(hass)
            await manager.async_update(
                {
                    "energy_sources": [
                        {
                            "type": "grid",
                            "stat_energy_from": "sensor.import_energy",
                            "stat_energy_to": "sensor.export_energy",
                        },
                        {"type": "solar", "stat_energy_from": "sensor.roof_energy"},
                    ],
                    "device_consumption": [],
                }
            )
            await hass.async_block_till_done()
            await coordinator.sync()
            assert coordinator.catalogue_discovery.healthy
            original_request = coordinator.client.request
            cached = await original_request("GET", "/v1/snapshot")
            now = dt_util.utcnow()
            plan = command_plan(now, powers=(0,))
            plan["config_revision"] = coordinator.schedule.config_revision
            plan["steps"][0]["devices"] = {}
            installed = None
            if caller == "confirmation_newer":
                installed = command_plan(now - timedelta(seconds=1), powers=(0,))
                installed["config_revision"] = coordinator.schedule.config_revision
                installed["steps"][0]["devices"] = {}
                assert coordinator.schedule.accept(installed, now)
            elif caller != "confirmation_empty":
                installed = plan
                assert coordinator.schedule.accept(plan, now)
            coordinator.refresh(now)
            assert coordinator.projection.status == (
                "not_ready" if caller == "confirmation_empty" else "active"
            )
            cached.update(plan=plan, plan_invalidated=False, planning={"status": "ready"})
            old_devices = deepcopy(cached["installation"]["devices"])
            assert old_devices and old_devices[0]["energy_entity"] == "sensor.roof_energy"
            confirmation = caller.startswith("confirmation_")
            accept = coordinator.schedule.accept
            fetched_plan_attempts = []

            def accept_reply(document, current):
                """Record attempts to accept the fetched plan after the genuine app rejection."""
                fetched_plan_attempts.append(document["plan_id"])
                return accept(document, current)

            if confirmation:
                monkeypatch.setattr(coordinator.schedule, "accept", accept_reply)

            async def request(method, path, body=None):
                """Model an old valid command reply or outage, preserving actual app catalogue traffic."""
                if path in ("/v1/snapshot", "/v1/observations"):
                    if caller == "recovery":
                        raise TimeoutError("Synthetic command transport failure")
                    return deepcopy(cached)
                return await original_request(method, path, body)

            monkeypatch.setattr(coordinator.client, "request", request)
            coordinator.catalogue_confirmed_at = now - timedelta(minutes=2)
            if caller == "normal":
                coordinator.catalogue_discovery.repair()
            active = True
            task = asyncio.create_task(coordinator.sync())
            try:
                async with asyncio.timeout(HTTP_GATE_TIMEOUT_SECONDS):
                    await entered.wait()
                unit = "kW" if caller == "recovery" or confirmation else "MW"
                hass.states.async_set("sensor.diagnostic_grid", "0", {"unit_of_measurement": unit})
                if confirmation:
                    roof = hass.states.get("sensor.roof_energy")
                    hass.states.async_set(
                        roof.entity_id,
                        roof.state,
                        {**roof.attributes, "friendly_name": "Updated roof"},
                    )
                await hass.async_block_till_done()
                release.set()
                await task
                assert len(responses) == 1
                membership = responses[0]
                assert membership["accepted"] is True
                assert membership["configuration_issue"] == "energy_configuration_changed"
                assert (
                    membership["devices"]
                    and membership["devices"][0]["energy_entity"] == "sensor.roof_energy"
                )
                assert not coordinator.catalogue_discovery.healthy
                assert coordinator.catalogue_confirmed_at is None
                if confirmation:
                    assert fetched_plan_attempts == []
                assert coordinator.schedule.plan is None
                assert coordinator.schedule.invalidated_generation == (
                    dt_util.parse_datetime(installed["generated_at"]) if installed else None
                )
                if not confirmation:
                    with pytest.raises(RevokedPlan):
                        coordinator.schedule.accept(plan, dt_util.utcnow())
                if caller == "normal" or confirmation:
                    assert coordinator.devices == membership["devices"]
                    assert coordinator.planning_status["reason"] == "energy_configuration_changed"
                    if confirmation:
                        assert membership["devices"] != old_devices
                        assert membership["devices"][0]["name"] == "Updated roof"
                else:
                    assert coordinator.issue == "synchronization_failed"
                accepted = await status(metadata=True)
                assert accepted["data"]["suggestions"]["solar_sources"] == ["sensor.roof_energy"]
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("correction", ["accepted", "unreachable", "changed_again"])
def test_watch_change_during_confirmation_cannot_leave_old_app_metadata_fresh(
    monkeypatch, correction
):
    """A genuine watched unit change during HTTP confirmation must reach the app before recovery ends."""
    from open_energy_planner.home_assistant.setup_catalogue import SetupDiscovery

    async def scenario():
        """Hold the authenticated receiver with Events and inspect its real accepted catalogue afterward."""
        entered, release = asyncio.Event(), asyncio.Event()
        original_discovery = SetupDiscovery.discovery
        hold = False

        async def discovery(owner, request):
            """Delay only a real confirmation before the existing receiver validates and accepts it."""
            confirming = not entered.is_set()
            if hold and not entered.is_set():
                entered.set()
                await release.wait()
            response = await original_discovery(owner, request)
            if hold and not confirming and correction == "changed_again":
                hass.states.async_set("sensor.diagnostic_grid", "0", {"unit_of_measurement": "MW"})
                await hass.async_block_till_done()
            return response

        monkeypatch.setattr(SetupDiscovery, "discovery", discovery)
        async with _accepted_metadata(measurement=True) as (hass, coordinator, status):
            original_request = coordinator.client.request
            corrections = []

            async def request(method, path, body=None):
                """Keep actual metadata HTTP traffic while command snapshots cannot be reached."""
                if path == "/v1/snapshot":
                    raise TimeoutError("Synthetic command transport failure")
                if path == "/v1/ha/catalogue" and "confirmed_revision" not in body:
                    corrections.append(body)
                    if correction == "unreachable":
                        raise ClientConnectionError("Synthetic correction transport failure")
                return await original_request(method, path, body)

            before = await status()
            accepted = await status(metadata=True)
            assert not accepted["stale"]
            assert accepted["data"]["entities"][0]["unit"] == "W"
            coordinator.catalogue_confirmed_at = dt_util.utcnow() - timedelta(minutes=2)
            monkeypatch.setattr(coordinator.client, "request", request)
            hold = True
            task = asyncio.create_task(coordinator.sync())
            try:
                async with asyncio.timeout(HTTP_GATE_TIMEOUT_SECONDS):
                    await entered.wait()
                hass.states.async_set("sensor.diagnostic_grid", "0", {"unit_of_measurement": "kW"})
                await hass.async_block_till_done()
                assert not coordinator.catalogue_discovery.healthy
                assert await status() == before
                release.set()
                await task
                after = await status()
                accepted = await status(metadata=True)
                assert coordinator.issue == "synchronization_failed"
                assert len(corrections) == 1
                if correction == "unreachable":
                    assert accepted["data"]["entities"][0]["unit"] == "W"
                    assert after["revision"] == before["revision"]
                    assert after["confirmed_at"] > before["confirmed_at"]
                    assert coordinator.catalogue_confirmed_at is None
                    assert not coordinator.catalogue_discovery.healthy
                else:
                    assert accepted["data"]["entities"][0]["unit"] == "kW"
                    assert after["revision"] != before["revision"]
                    if correction == "changed_again":
                        assert coordinator.catalogue_context.revision != after["revision"]
                        assert coordinator.catalogue_confirmed_at is None
                        assert not coordinator.catalogue_discovery.healthy
                    else:
                        assert coordinator.catalogue_context.revision == after["revision"]
                        assert coordinator.catalogue_discovery.healthy
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_path", ["/v1/snapshot", "/v1/observations"])
def test_http_validation_rejection_does_not_confirm_metadata(monkeypatch, failure_path):
    """An actual HTTP 400 rejection must not be treated as an unavailable transport."""

    async def scenario():
        """Use the real report validator and client HTTP exception for either command read boundary."""
        async with _accepted_metadata(measurement=True) as (_hass, coordinator, status):
            original = coordinator.client.request
            rejections, confirmations = [], []

            async def request(method, path, body=None):
                """Return a genuine app-side HTTP validation rejection with otherwise healthy transport."""
                if path == failure_path:
                    try:
                        return await original("POST", "/v1/observations", {"invalid_report": True})
                    except ClientResponseError as error:
                        rejections.append(error.status)
                        raise
                if path == "/v1/ha/catalogue":
                    confirmations.append(body)
                return await original(method, path, body)

            before = await status()
            coordinator.catalogue_confirmed_at = dt_util.utcnow() - timedelta(minutes=2)
            monkeypatch.setattr(coordinator.client, "request", request)
            await coordinator.sync()
            assert rejections == [400]
            assert coordinator.issue == "synchronization_failed"
            assert confirmations == []
            assert await status() == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "identities",
    [
        None,
        "sensor.missing",
        [None],
        [1],
        ["switch.unsupported"],
        ["sensor." + "x" * 121],
        ["sensor.missing"],
    ],
)
def test_command_outage_cannot_confirm_missing_or_malformed_current_sources(
    monkeypatch, identities
):
    """The actual app rejects current source faults atomically before updating confirmed provenance."""
    import open_energy_planner.app as app_module

    async def scenario():
        """Change the authoritative projection only when the confirmation reaches the real receiver."""
        async with _accepted_metadata() as (_hass, coordinator, status):
            original = coordinator.client.request
            rejected = []

            async def request(method, path, body=None):
                """Keep real HTTP rejection and introduce a command-only transport outage."""
                if path == "/v1/snapshot":
                    raise TimeoutError("Synthetic command timeout")
                if path == "/v1/ha/catalogue":
                    assert set(body) == {"confirmed_revision"}
                    monkeypatch.setattr(app_module, "configured_sources", lambda *_args: identities)
                    try:
                        return await original(method, path, body)
                    except ClientResponseError as error:
                        rejected.append(error.status)
                        raise
                raise AssertionError("Recovery must not publish metadata or forecasts")

            before = await status()
            coordinator.catalogue_confirmed_at = dt_util.utcnow() - timedelta(minutes=2)
            monkeypatch.setattr(coordinator.client, "request", request)
            await coordinator.sync()
            assert rejected == [400]
            assert await status() == before
            assert not coordinator.catalogue_discovery.healthy
            assert coordinator.issue == "synchronization_failed"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure_type",
    [
        ValueError,
        TypeError,
        KeyError,
        AuthenticationError,
        ReleaseMismatchError,
        ServerFingerprintMismatch,
        asyncio.CancelledError,
    ],
)
def test_nontransport_failures_never_use_metadata_confirmation(monkeypatch, failure_type):
    """Validation, authentication and release failures cannot authorize the transport recovery path."""

    async def scenario():
        """Retain genuine accepted metadata while failing the initial command response by category."""
        async with _accepted_metadata() as (_hass, coordinator, status):
            requests = []

            async def request(method, path, body=None):
                """Record every attempted route and reject the command read."""
                requests.append(path)
                if failure_type is ServerFingerprintMismatch:
                    raise ServerFingerprintMismatch(b"a" * 32, b"b" * 32, "127.0.0.1", 8099)
                raise failure_type("Synthetic command rejection")

            before = await status()
            coordinator.catalogue_confirmed_at = dt_util.utcnow() - timedelta(minutes=2)
            monkeypatch.setattr(coordinator.client, "request", request)
            if failure_type is asyncio.CancelledError:
                with pytest.raises(asyncio.CancelledError):
                    await coordinator.sync()
            else:
                await coordinator.sync()
            assert requests == ["/v1/snapshot"]
            assert await status() == before

    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable", ["changed_watch", "rejected_bindings", "closed"])
def test_unavailable_watch_cannot_confirm_after_command_failure(monkeypatch, unavailable):
    """Changed, rejected or closed watches retain the last confirmation without additional HTTP work."""

    async def scenario():
        """Exercise the existing local eligibility guards without replacing the acknowledgement context."""
        async with _accepted_metadata() as (_hass, coordinator, status):
            requests = []

            async def request(method, path, body=None):
                """A command transport outage must not become a metadata publication request."""
                requests.append(path)
                raise TimeoutError("Synthetic command timeout")

            before = await status()
            coordinator.catalogue_confirmed_at = dt_util.utcnow() - timedelta(minutes=2)
            monkeypatch.setattr(coordinator.client, "request", request)
            if unavailable == "changed_watch":
                coordinator.catalogue_discovery.repair()
            elif unavailable == "rejected_bindings":
                coordinator.catalogue_bindings_rejected = True
            else:
                coordinator.closed = True
            try:
                await coordinator.sync()
            finally:
                coordinator.closed = False
            assert requests == ([] if unavailable == "closed" else ["/v1/snapshot"])
            assert await status() == before

    asyncio.run(scenario())


@pytest.mark.parametrize("finish", ["cancel", "close", "missing_gate"])
def test_inflight_confirmation_does_not_reopen_cancelled_or_closed_owner(monkeypatch, finish):
    """Cancellation propagates and a late accepted reply cannot advance a closed coordinator."""

    async def scenario():
        """Gate one actual confirmation at its HTTP boundary and retain the original local clock."""
        async with _accepted_metadata() as (_hass, coordinator, _status):
            entered, release = asyncio.Event(), asyncio.Event()
            original = coordinator.client.request

            async def request(method, path, body=None):
                """Wait before the actual confirmation, retaining the same authenticated receiver."""
                if finish != "missing_gate":
                    entered.set()
                await release.wait()
                return await original(method, path, body)

            coordinator.catalogue_confirmed_at = dt_util.utcnow() - timedelta(minutes=2)
            before = coordinator.catalogue_confirmed_at
            monkeypatch.setattr(coordinator.client, "request", request)
            task = asyncio.create_task(coordinator._confirm_catalogue(dt_util.utcnow()))
            try:
                if finish == "missing_gate":
                    with pytest.raises(TimeoutError):
                        async with asyncio.timeout(0):
                            await entered.wait()
                else:
                    async with asyncio.timeout(HTTP_GATE_TIMEOUT_SECONDS):
                        await entered.wait()
                    if finish == "cancel":
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                    else:
                        coordinator.closed = True
                        release.set()
                        await task
                assert coordinator.catalogue_confirmed_at == before
            finally:
                release.set()
                coordinator.closed = False
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert task.done()
            if finish == "missing_gate":
                assert task.cancelled()

    asyncio.run(scenario())


def test_command_outage_cannot_confirm_a_replaced_revision(monkeypatch):
    """A stale acknowledged revision is rejected by the real receiver and requires full recovery."""

    async def scenario():
        """Keep the server revision intact while the peer attempts an obsolete context."""
        async with _accepted_metadata() as (_hass, coordinator, status):
            original = coordinator.client.request

            async def request(method, path, body=None):
                """Fail only command reads and preserve actual revision validation over HTTP."""
                if path == "/v1/snapshot":
                    raise TimeoutError("Synthetic command timeout")
                return await original(method, path, body)

            before = await status()
            coordinator.catalogue_context = replace(
                coordinator.catalogue_context, revision="0" * 64
            )
            coordinator.catalogue_confirmed_at = dt_util.utcnow() - timedelta(minutes=2)
            monkeypatch.setattr(coordinator.client, "request", request)
            await coordinator.sync()
            assert await status() == before
            assert not coordinator.catalogue_discovery.healthy
            assert coordinator.issue == "synchronization_failed"

    asyncio.run(scenario())
