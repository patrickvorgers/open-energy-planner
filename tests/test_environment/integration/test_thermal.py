"""Independent thermal laboratory conservation, persistence and blueprint execution proofs."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("homeassistant", reason="Thermal fixture tests run in the pinned HA container")

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from test_environment.oep_test_fixture.thermal import BoilerSimulation, async_setup, live_extra
from tests.custom_component.support.chain import running_chain


def test_boiler_energy_losses_draw_and_safe_restart(tmp_path):
    """A 200 L tank conserves electricity and heat, cools on withdrawal and never restores an old ON request."""

    async def scenario():
        """Compare metered energy and temperatures with independent heat-capacity calculations."""
        hass = HomeAssistant(str(tmp_path))
        hass.config.time_zone = "Europe/Amsterdam"
        ids = {
            key: ("switch." if key == "heater" else "sensor.") + "oep_test_boiler_" + key
            for key in ("temperature", "power", "energy", "heater")
        }
        store = SimpleNamespace(async_save=AsyncMock())
        try:
            tank = BoilerSimulation(hass, None, store, None, ids)
            tank.at = dt_util.parse_datetime("2026-10-27T11:00:00+00:00")
            tank.requested = True
            tank.advance(tank.at + timedelta(minutes=30))
            assert tank.energy_kwh == pytest.approx(1, abs=1e-9)
            capacity_kwh_per_c = 200 * 4186 / 3_600_000
            stored_kwh = (tank.temperature_c - 55) * capacity_kwh_per_c
            assert 0.0175 < tank.energy_kwh - stored_kwh < 0.02
            assert tank.power_w == 2000
            tank.requested = False
            stopped_energy = tank.energy_kwh
            tank.advance(tank.at + timedelta(minutes=30))
            assert tank.energy_kwh == stopped_energy and tank.power_w == 0
            assert (tank.temperature_c - 55) * capacity_kwh_per_c < stored_kwh
            restored = BoilerSimulation(
                hass, None, store, {"temperature_c": 61, "energy_kwh": 42, "requested": True}, ids
            )
            assert restored.temperature_c == 61 and restored.energy_kwh == 42
            assert not restored.requested and restored.power_w == 0
            restored.at = dt_util.parse_datetime("2026-10-27T06:00:00+00:00")
            restored.advance(restored.at + timedelta(minutes=15))
            expected_without_losses = 61 - 0.8 / capacity_kwh_per_c
            assert expected_without_losses - 0.1 < restored.temperature_c < expected_without_losses
            assert restored.energy_kwh == 42
            await restored.save()
            assert store.async_save.call_args.args[0] == {
                "temperature_c": restored.temperature_c,
                "energy_kwh": 42,
            }
            restored.temperature_c = 64.99
            restored.requested = True
            restored.thermostat_on = True
            restored.at = dt_util.parse_datetime("2026-10-27T11:00:00+00:00")
            restored.advance(restored.at + timedelta(minutes=30))
            assert 64 < restored.temperature_c <= 65
            assert restored.power_w == 0
            assert restored.energy_kwh - 42 < 0.01
        finally:
            await hass.async_stop()

    asyncio.run(scenario())


def test_blueprint_executes_only_laboratory_actions_and_stops_on_lost_permission():
    """The real HA blueprint enables the simulated heater and stops on off, unknown and unavailable."""

    async def scenario():
        """Load actual blueprint automation and retain all pre-existing Energy consumer configuration."""
        from homeassistant.components.energy.data import async_get_manager

        async with running_chain(live=True, recorder=True) as (hass, entry, _url, _root):
            from test_environment.oep_test_fixture import thermal

            manager = await async_get_manager(hass)
            hass.states.async_set(
                "sensor.prior_energy",
                123.45,
                {
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                },
            )
            await manager.async_update(
                {"device_consumption": [{"stat_consumption": "sensor.prior_energy"}]}
            )
            before = list((manager.data or {}).get("device_consumption", []))
            with patch.object(
                thermal, "import_fine", new=AsyncMock(wraps=thermal.import_fine)
            ) as import_history:
                tank, concurrent = await asyncio.gather(
                    async_setup(hass, entry), async_setup(hass, entry)
                )
                assert tank is concurrent
                assert tank is await async_setup(hass, entry)
                import_history.assert_awaited_once()
                imported_hass, metadata, fine, hourly = import_history.call_args.args
                assert imported_hass is hass
                assert metadata["statistic_id"] == "sensor.oep_test_boiler_energy"
                assert len(hourly) >= 35 * 24 and len(fine) >= 35 * 24 * 12
                assert all(row["sum"] == 0 and row["state"] == 0 for row in [*fine, *hourly])
                assert hass.states.get("sensor.prior_energy").state == "123.45"
            assert all(row in manager.data["device_consumption"] for row in before)
            assert (
                sum(
                    row["stat_consumption"] == tank.ids["energy"]
                    for row in manager.data["device_consumption"]
                )
                == 1
            )
            assert tank.ids["temperature"] == "sensor.oep_test_boiler_temperature"
            assert tank.ids["power"] == "sensor.oep_test_boiler_power"
            assert tank.ids["energy"] == "sensor.oep_test_boiler_energy"
            permission = "binary_sensor.test_boiler_permission"
            hass.states.async_set(permission, "off")
            assert await async_setup_component(
                hass,
                "automation",
                {
                    "automation": [
                        {
                            "id": "thermal_fixture_chain",
                            "alias": "Thermal fixture chain",
                            "use_blueprint": {
                                "path": "open_energy_planner/thermal.yaml",
                                "input": {
                                    "permission": permission,
                                    "heat_action": [
                                        {
                                            "action": "oep_test_fixture.boiler_heater",
                                            "data": {"enabled": True},
                                        }
                                    ],
                                    "stop_action": [
                                        {
                                            "action": "oep_test_fixture.boiler_heater",
                                            "data": {"enabled": False},
                                        }
                                    ],
                                },
                            },
                        }
                    ]
                },
            )
            await hass.async_block_till_done()
            for withdrawn in ("off", "unknown", "unavailable"):
                hass.states.async_set(permission, "on")
                await hass.async_block_till_done()
                assert tank.requested and tank.power_w == 2000
                assert hass.states.get(tank.ids["heater"]).state == "on"
                hass.states.async_set(permission, withdrawn)
                await hass.async_block_till_done()
                assert not tank.requested and tank.power_w == 0
            hass.states.async_set(permission, "on")
            await hass.async_block_till_done()
            tank.requested = False
            hass.bus.async_fire("automation_reloaded")
            await hass.async_block_till_done()
            assert tank.requested
            hass.states.async_set(permission, "unavailable")
            await hass.async_block_till_done()
            assert live_extra(hass)[1] == 0

    asyncio.run(scenario())
