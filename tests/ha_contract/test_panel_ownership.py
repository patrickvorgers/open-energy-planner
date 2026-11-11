"""Retain accepted HA panel ownership when discovery cannot confirm an unlink."""

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.home_assistant.solar_panels import follow_panels, panel_settings
from open_energy_planner.solar.calculation.model import SolarImport, SolarPanel, SolarSource


@pytest.mark.parametrize("discovery_status", ["stale", "missing", "not_ready", "omitted"])
@pytest.mark.parametrize("accepted", ["following", "manual", "none"])
def test_unknown_discovery_cannot_release_accepted_panel_ownership(discovery_status, accepted):
    """Only a fresh complete explicit unlink can release geometry retained in an accepted HA snapshot."""
    plane = SolarPanel("garden", 2400, 110, 20)
    source = SolarSource(
        "garden",
        "sensor.garden",
        "converter",
        2400,
        "east",
        panels=(plane,),
        ha_imports=(SolarImport("forecast_solar", "garden", (plane,)),),
    )
    catalogue = {
        "stale": discovery_status == "stale",
        "data": {
            "suggestions": {
                "energy_status": "incomplete" if discovery_status == "not_ready" else "ready",
                "solar_configurations": []
                if discovery_status == "omitted"
                else [
                    {
                        "entity_id": "sensor.garden",
                        "issue": "solar_configuration_not_available",
                        "providers": [],
                    }
                ],
            }
        },
    }
    if discovery_status == "missing":
        catalogue["data"] = None
    previous = (
        source
        if accepted == "following"
        else replace(source, ha_imports=())
        if accepted == "manual"
        else None
    )
    retained = panel_settings(catalogue, source.entity_id, previous)
    assert retained.document() == {"state": "unavailable", "managed": True}
    assert follow_panels(source, retained) == source


def test_pending_catalogue_cannot_confirm_or_replace_accepted_panel_context(tmp_path, monkeypatch):
    """Only a completed transfer or matching current revision can advance accepted metadata freshness."""
    from custom_components.open_energy_planner import catalogue_transfer
    from open_energy_planner.app import create_app
    from tests.ha_contract.support.metadata import setup_metadata

    async def scenario():
        """Send actual ordered pages and reject confirmation while the new snapshot is incomplete."""
        token = "panel-test-token-0123456789abcdef"
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            headers = {"Authorization": "Bearer " + token}
            body = setup_metadata()
            response = await client.post("/v1/ha/catalogue", headers=headers, json=body)
            assert response.status == 200
            before = await (await client.get("/v1/ha/catalogue/refresh", headers=headers)).json()
            for index in range(100):
                entity = deepcopy(body["entities"][0])
                entity["entity_id"] = f"sensor.panel_diagnostic_{index}"
                body["entities"].append(entity)
            monkeypatch.setattr(catalogue_transfer, "CATALOGUE_PAGE_BYTES", 8192)
            pages = catalogue_transfer.prepare_catalogue(body).pages
            assert len(pages) > 2
            response = await client.post("/v1/ha/catalogue", headers=headers, json=pages[0])
            assert response.status == 200
            assert await response.json() == {"accepted": False}
            response = await client.post(
                "/v1/ha/catalogue", headers=headers, json={"confirmed_revision": before["revision"]}
            )
            assert response.status == 400
            assert (
                await (await client.get("/v1/ha/catalogue/refresh", headers=headers)).json()
                == before
            )
            for page in pages[1:]:
                response = await client.post("/v1/ha/catalogue", headers=headers, json=page)
                assert response.status == 200
            assert (await response.json())["accepted"] is True
            after = await (await client.get("/v1/ha/catalogue/refresh", headers=headers)).json()
            assert after["revision"] != before["revision"]
            response = await client.post(
                "/v1/ha/catalogue", headers=headers, json={"confirmed_revision": before["revision"]}
            )
            assert response.status == 400
            assert (
                await (await client.get("/v1/ha/catalogue/refresh", headers=headers)).json()
                == after
            )
            response = await client.post(
                "/v1/ha/catalogue", headers=headers, json={"confirmed_revision": after["revision"]}
            )
            assert response.status == 200
            final = await (await client.get("/v1/ha/catalogue/refresh", headers=headers)).json()
            assert final["published_at"] == after["published_at"]
            assert final["confirmed_at"] > after["confirmed_at"]

    asyncio.run(scenario())
