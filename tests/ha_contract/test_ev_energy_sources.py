"""Keep charge-point measurement bindings identical to their Home Assistant Energy device."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from open_energy_planner.appliances.ev.configuration import configuration
from tests.app.ev.support.devices import catalogue, settings


def test_ev_page_read_includes_discovered_plug_without_changing_active_sources(tmp_path):
    """Reloading EV settings retains a fresh no-device plug helper from the accepted chooser."""
    from aiohttp import ClientSession

    from open_energy_planner.app import create_app
    from tests.ha_contract.support.connection import TOKEN, server
    from tests.ha_contract.support.metadata import setup_metadata

    async def scenario():
        """Read the real EV endpoint before and after a bounded chooser-only capture."""
        active = setup_metadata()
        candidates = deepcopy(active)
        candidates["entities"].append(
            {
                "entity_id": "binary_sensor.garage_plug",
                "name": "Garage plug",
                "device_id": "",
                "unit": "",
                "device_class": "plug",
                "state_class": "",
            }
        )
        async with (
            server(create_app(tmp_path, TOKEN, source="home_assistant")) as url,
            ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as client,
        ):
            async with client.post(url + "/v1/ha/catalogue", json=active) as response:
                assert response.status == 200
            async with client.post(url + "/v1/ha/source-choices/ev", json={}) as response:
                nonce = (await response.json())["id"]
            async with client.post(
                url + "/v1/ha/source-choices",
                json={"scope": "ev", "id": nonce, "page": candidates},
            ) as response:
                assert (await response.json())["accepted"]
            async with client.get(url + "/v1/ev") as response:
                document = await response.json()
            assert "binary_sensor.garage_plug" in {
                row["entity_id"] for row in document["catalogue"]["entities"]
            }
            async with client.get(url + "/v1/thermal") as response:
                document = await response.json()
            assert "binary_sensor.garage_plug" not in {
                row["entity_id"] for row in document["catalogue"]["entities"]
            }

    asyncio.run(scenario())


@pytest.mark.parametrize("linked_power", [None, "sensor.other_power"])
def test_energy_power_link_is_required_and_cannot_be_overridden(linked_power):
    """Missing or different Energy power links reject a valid-looking manual power sensor."""
    consumer = {"entity_id": "sensor.charger_energy"}
    if linked_power:
        consumer["power_entity"] = linked_power
    consumers = SimpleNamespace(options=[consumer], selected=[])
    with pytest.raises(ValueError, match="Home Assistant Energy"):
        configuration(settings(), catalogue(), consumers, SimpleNamespace(state={"devices": {}}))


def test_missing_energy_meter_is_rejected_by_membership_validation():
    """Valid sensor metadata cannot admit a charge point whose meter is absent from Energy."""
    consumers = SimpleNamespace(options=[], selected=[])
    with pytest.raises(ValueError, match="^Select consumers configured in Home Assistant Energy$"):
        configuration(settings(), catalogue(), consumers, SimpleNamespace(state={"devices": {}}))
