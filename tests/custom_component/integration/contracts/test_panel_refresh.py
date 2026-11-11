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


@asynccontextmanager
async def _accepted_metadata(*, measurement=False):
    """Own real HA watches and an authenticated app with a previously acknowledged catalogue."""
    async with running_chain(live=True) as (hass, entry, url, _root):
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

            async def status():
                """Read provenance without implicitly requesting a metadata refresh."""
                async with session.get(url + "/v1/ha/catalogue/refresh") as response:
                    assert response.status == 200
                    return await response.json()

            yield hass, coordinator, status


@pytest.mark.parametrize("failure_path", ["/v1/snapshot", "/v1/observations"])
@pytest.mark.parametrize("failure_type", [TimeoutError, ClientConnectionError])
def test_healthy_panel_metadata_confirmation_survives_command_snapshot_timeout(
    monkeypatch, failure_path, failure_type
):
    """A failed command snapshot must not prevent confirming unchanged watched source metadata."""

    async def scenario():
        """Keep real HA watches and authenticated app confirmation while one command read times out."""
        async with _accepted_metadata() as (_hass, coordinator, status):
            original_request = coordinator.client.request
            confirmations = []

            async def request(method, path, body=None):
                """Fail only the command snapshot, retaining every other actual app HTTP request."""
                if path == failure_path:
                    raise failure_type("Synthetic command transport failure")
                if path == "/v1/ha/catalogue" and "confirmed_revision" in (body or {}):
                    confirmations.append(body)
                data = await original_request(method, path, body)
                if path == "/v1/snapshot" and failure_path == "/v1/observations":
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


@pytest.mark.parametrize("finish", ["cancel", "close"])
def test_inflight_confirmation_does_not_reopen_cancelled_or_closed_owner(monkeypatch, finish):
    """Cancellation propagates and a late accepted reply cannot advance a closed coordinator."""

    async def scenario():
        """Gate one actual confirmation at its HTTP boundary and retain the original local clock."""
        async with _accepted_metadata() as (_hass, coordinator, _status):
            entered, release = asyncio.Event(), asyncio.Event()
            original = coordinator.client.request

            async def request(method, path, body=None):
                """Wait before the actual confirmation, retaining the same authenticated receiver."""
                entered.set()
                await release.wait()
                return await original(method, path, body)

            coordinator.catalogue_confirmed_at = dt_util.utcnow() - timedelta(minutes=2)
            before = coordinator.catalogue_confirmed_at
            monkeypatch.setattr(coordinator.client, "request", request)
            task = asyncio.create_task(coordinator._confirm_catalogue(dt_util.utcnow()))
            await entered.wait()
            try:
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
                await asyncio.gather(task, return_exceptions=True)

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
