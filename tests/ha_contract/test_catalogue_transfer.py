"""Prove complete discovery publication and retention across interrupted snapshot transfers."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.home_assistant.catalogue import HACatalogue
from tests.ha_contract.support.metadata import setup_metadata


def test_http_discovery_publishes_only_complete_valid_membership(tmp_path):
    """Real HTTP fragments cannot change visible sources or Energy consumers until final validation."""
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.app import create_app

    async def scenario():
        """Read the actual EV and consumer APIs between each authenticated discovery request."""
        token = "synthetic-transfer-admin-0123456789"
        application = create_app(tmp_path, token, source="home_assistant")
        async with TestClient(TestServer(application)) as client:
            headers = {"Authorization": "Bearer " + token}
            previous = setup_metadata()
            response = await client.post("/v1/ha/catalogue", json=previous, headers=headers)
            assert response.status == 200
            consumer_response = await client.get("/v1/consumers", headers=headers)
            prior_consumers = await consumer_response.json()
            newer = deepcopy(previous)
            newer["entities"][0]["name"] = "Refreshed grid meter"
            pages = snapshot_pages(newer, datetime.now(UTC) + timedelta(seconds=1))
            for page in pages[:-1]:
                response = await client.post("/v1/ha/catalogue", json=page, headers=headers)
                assert response.status == 200
                assert (await response.json())["accepted"] is False
                state_response = await client.get("/v1/ev", headers=headers)
                assert (await state_response.json())["catalogue"] == previous
                consumer_response = await client.get("/v1/consumers", headers=headers)
                assert (await consumer_response.json())["version"] == prior_consumers["version"]
            response = await client.post("/v1/ha/catalogue", json=pages[-1], headers=headers)
            assert response.status == 200
            assert (await response.json())["accepted"] is True
            state_response = await client.get("/v1/ev", headers=headers)
            assert (await state_response.json())["catalogue"] == newer

    asyncio.run(scenario())


def test_pending_catalogue_transfer_retains_an_aggregate_memory_budget(monkeypatch):
    """Several individually small fragments cannot consume unlimited assembly memory."""
    from open_energy_planner.home_assistant import catalogue_transfer

    now = datetime(2026, 11, 7, tzinfo=UTC)
    catalogue = HACatalogue()
    previous = setup_metadata()
    catalogue.receive(previous, now)
    larger = deepcopy(previous)
    larger["entities"] *= 30
    pages = snapshot_pages(larger, now + timedelta(seconds=1))
    monkeypatch.setattr(catalogue_transfer, "CATALOGUE_TRANSFER_BYTES", 4096)
    with pytest.raises(ValueError, match="Invalid catalogue transfer"):
        for page in pages:
            catalogue.receive_page(page, now + timedelta(seconds=2))
    assert catalogue.value == previous


def snapshot_pages(body, captured, identity="synthetic-transfer"):
    """Describe an independent ordered wire exchange, with each original entity on its own page."""
    revision = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    header = deepcopy(body)
    entities = header.pop("entities")
    header["entities"] = []
    payloads = [{"header": header}]
    payloads.extend({"path": ["entities"], "items": [entity]} for entity in entities)
    payloads.append({})
    return [
        {
            "transfer": {
                "id": identity,
                "revision": revision,
                "captured_at": captured.isoformat(),
                "index": index,
                "complete": index == len(payloads) - 1,
            },
            **payload,
        }
        for index, payload in enumerate(payloads)
    ]


def test_partial_catalogue_keeps_previous_snapshot_until_complete_validation():
    """No partial entities or changed Energy membership become visible before the final digest."""
    now = datetime(2026, 11, 7, tzinfo=UTC)
    catalogue = HACatalogue()
    previous = setup_metadata()
    catalogue.receive(previous, now)
    newer = deepcopy(previous)
    newer["entities"][-1]["name"] = "Replacement meter"
    pages = snapshot_pages(newer, now + timedelta(seconds=1))
    for page in pages[:-1]:
        assert catalogue.receive_page(page, now + timedelta(seconds=2)) is None
        assert catalogue.value == previous
        assert catalogue.updated == now
    completed = catalogue.receive_page(pages[-1], now + timedelta(seconds=2))
    assert completed == newer
    catalogue.receive(completed, now + timedelta(seconds=2))
    assert catalogue.value == newer


@pytest.mark.parametrize("failure", ["order", "identity", "digest", "path", "expired", "stale"])
def test_invalid_transfer_never_replaces_last_complete_catalogue(failure):
    """Out-of-order, mixed, corrupt, unexpected, expired and stale pages retain prior discovery."""
    now = datetime(2026, 11, 7, tzinfo=UTC)
    catalogue = HACatalogue()
    previous = setup_metadata()
    catalogue.receive(previous, now)
    pages = snapshot_pages(previous, now + timedelta(seconds=1))
    received = now + timedelta(seconds=2)
    assert catalogue.receive_page(pages[0], received) is None
    bad = deepcopy(pages[1])
    if failure == "order":
        bad["transfer"]["index"] = 2
    elif failure == "identity":
        bad["transfer"]["id"] = "different-snapshot"
    elif failure == "path":
        bad["path"] = ["arbitrary", "configuration"]
    elif failure == "expired":
        received += timedelta(minutes=6)
    elif failure == "stale":
        bad = pages[0]
    elif failure == "digest":
        bad["items"][0]["name"] = "Tampered metadata"
        assert catalogue.receive_page(bad, received) is None
        assert catalogue.receive_page(pages[2], received) is None
        bad = pages[-1]
    with pytest.raises(ValueError):
        catalogue.receive_page(bad, received)
    assert catalogue.value == previous
    assert catalogue.updated == now
    assert (
        catalogue.receive_page(
            snapshot_pages(previous, now + timedelta(minutes=7), "recovery")[0],
            now + timedelta(minutes=7),
        )
        is None
    )


def test_newer_transfer_supersedes_incomplete_snapshot_without_accepting_old_pages():
    """A newer start replaces pending state, and delayed older fragments cannot mix into it."""
    now = datetime(2026, 11, 7, tzinfo=UTC)
    catalogue = HACatalogue()
    previous = setup_metadata()
    catalogue.receive(previous, now)
    old = snapshot_pages(previous, now + timedelta(seconds=1), "older")
    new = snapshot_pages(previous, now + timedelta(seconds=2), "newer")
    assert catalogue.receive_page(old[0], now + timedelta(seconds=3)) is None
    assert catalogue.receive_page(new[0], now + timedelta(seconds=3)) is None
    with pytest.raises(ValueError):
        catalogue.receive_page(old[1], now + timedelta(seconds=3))
    assert catalogue.value == previous
