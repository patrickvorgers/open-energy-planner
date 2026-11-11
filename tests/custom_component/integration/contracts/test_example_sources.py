"""Verify example provisioning against genuine scoped HA discovery and product validation."""

import asyncio
from copy import deepcopy

import pytest

from script.provisioning import device_examples as examples
from test_environment.oep_test_fixture.ev import setup_ev
from test_environment.oep_test_fixture.thermal import async_setup as setup_thermal
from tests.custom_component.support.chain import TOKEN, configure_energy, running_chain


@pytest.mark.parametrize("existing_power", [None, "sensor.custom_power"])
def test_ev_fixture_fills_only_missing_owned_energy_power_links(existing_power):
    """Native Energy links cover new and retained fixture points without replacing explicit or unrelated choices."""
    from homeassistant.components.energy.data import (
        DEVICE_CONSUMPTION_SCHEMA,
        async_get_manager,
    )
    from homeassistant.helpers import entity_registry as er

    from test_environment.oep_test_fixture.ev import DATA_KEY

    async def scenario():
        """Preserve existing preference rows while validating the simulator's native meter/power pairs."""
        async with running_chain(recorder=True) as (hass, entry, _url, _root):
            energy = (
                er.async_get(hass)
                .async_get_or_create(
                    "sensor",
                    "oep_test_fixture",
                    "ev_wallbox_a_energy",
                    config_entry=entry,
                    suggested_object_id="retained_fixture_energy",
                )
                .entity_id
            )
            retained = {"stat_consumption": energy, "name": "Retained fixture name"}
            if existing_power:
                retained["stat_rate"] = existing_power
            foreign = {
                "stat_consumption": "sensor.unrelated_energy",
                "stat_rate": "sensor.unrelated_power",
                "name": "Unrelated device",
            }
            manager = await async_get_manager(hass)
            await manager.async_update({"device_consumption": [retained, foreign]})
            await setup_ev(hass, entry)
            points = hass.data[DATA_KEY]["points"]
            rows = manager.data["device_consumption"]
            assert len(rows) == 3
            assert rows[1] == foreign
            for point_index, point in enumerate(points):
                row = next(row for row in rows if row["stat_consumption"] == point["energy"])
                assert DEVICE_CONSUMPTION_SCHEMA(row) == row
                assert row["stat_rate"] == (
                    existing_power if point_index == 0 and existing_power else point["power"]
                )
            assert rows[0]["name"] == "Retained fixture name"
            saved = deepcopy(manager.data)
            await setup_ev(hass, entry)
            assert manager.data == saved

    asyncio.run(scenario())


def test_example_choices_enable_versioned_saves_without_broadening_active_sources():
    """Energy-linked powers are active while other EV and boiler sources need normal saves."""

    async def scenario():
        """Exercise authenticated HTTP, real HA captures and accepted app configuration without hardware."""
        async with running_chain(live=True, recorder=True, peer_gateway=True) as (
            hass,
            entry,
            url,
            _root,
        ):
            hass.states.async_set(
                "sensor.grid", "0", {"unit_of_measurement": "W", "device_class": "power"}
            )
            hass.states.async_set(
                "sensor.import",
                "100",
                {
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                },
            )
            await configure_energy(
                hass,
                entry,
                [{"type": "grid", "stat_energy_from": "sensor.import", "stat_rate": "sensor.grid"}],
            )
            await setup_ev(hass, entry)
            await setup_thermal(hass, entry)
            coordinator = entry.runtime_data
            coordinator.catalogue_sent = None
            await coordinator.sync()
            extra_sources = {
                "ev": {
                    "sensor.oep_test_car_a_soc",
                    "sensor.oep_test_car_b_soc",
                    "sensor.oep_test_wallbox_a_power",
                    "sensor.oep_test_wallbox_b_power",
                    "binary_sensor.oep_test_wallbox_a_connected",
                    "binary_sensor.oep_test_wallbox_b_connected",
                },
                "thermal": {
                    "sensor.oep_test_boiler_power",
                    "sensor.oep_test_boiler_temperature",
                },
            }
            async with examples.product_requests(app_url=url, token=TOKEN) as product_request:

                async def request(method, path, body=None):
                    """Complete the real integration poll after each explicit finite chooser request."""
                    result = await product_request(method, path, body)
                    if method == "POST" and path.startswith("/v1/ha/source-choices/"):
                        await coordinator.sync()
                    return result

                before = (await request("GET", "/v1/setup"))["catalogue"]["data"]
                active_before = {entity["entity_id"] for entity in before["entities"]}
                energy_powers = {
                    "sensor.oep_test_wallbox_a_power",
                    "sensor.oep_test_wallbox_b_power",
                }
                assert active_before & set.union(*extra_sources.values()) == energy_powers
                for scope, required in extra_sources.items():
                    await examples.ensure_source_choices(request, scope, required)
                    assert (await request("GET", "/v1/setup"))["catalogue"]["data"] == before
                for scope, configuration in (
                    ("ev", examples.ev_configuration()),
                    ("thermal", examples.boiler_configuration()),
                ):
                    current = await request("GET", "/v1/" + scope)
                    await request(
                        "POST",
                        "/v1/" + scope + "/configuration",
                        {
                            "configuration": configuration,
                            "expected_version": current["configuration_version"],
                        },
                    )
                await coordinator.sync()
                active = (await request("GET", "/v1/setup"))["catalogue"]["data"]
                assert set.union(*extra_sources.values()) <= {
                    entity["entity_id"] for entity in active["entities"]
                }
                assert len((await request("GET", "/v1/ev"))["vehicles"]) == 2
                assert len((await request("GET", "/v1/thermal"))["configuration"]["stores"]) == 1
                for entity in (
                    "sensor.oep_test_wallbox_a_power",
                    "sensor.oep_test_wallbox_b_power",
                    "sensor.oep_test_boiler_power",
                ):
                    assert float(hass.states.get(entity).state) == 0

    asyncio.run(scenario())
