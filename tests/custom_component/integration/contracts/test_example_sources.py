"""Verify example provisioning against genuine scoped HA discovery and product validation."""

import asyncio

from script.provisioning import device_examples as examples
from test_environment.oep_test_fixture.ev import setup_ev
from test_environment.oep_test_fixture.thermal import async_setup as setup_thermal
from tests.custom_component.support.chain import TOKEN, configure_energy, running_chain


def test_example_choices_enable_versioned_saves_without_broadening_active_sources():
    """Simulator EV and boiler sources become active only after complete choices and normal saves."""

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
                assert not {entity["entity_id"] for entity in before["entities"]} & set.union(
                    *extra_sources.values()
                )
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
