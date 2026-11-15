"""Native Recorder row limits, original state semantics and physically retained executor admission."""

import asyncio
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant", reason="Native Recorder proof runs in the pinned HA image")

from aiohttp import ClientSession
from homeassistant.components.recorder import get_instance
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.open_energy_planner import appliance_history, appliance_raw_history
from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.cycles.raw import analyze
from tests.app.appliance.raw_support import raw_trace
from tests.app.appliance.support import ENTITY, POWER
from tests.custom_component.support.chain import TOKEN, configure_energy, running_chain


def test_native_recorder_retains_original_times_unit_changes_and_explicit_limited_tail(monkeypatch):
    """Real SQL reads keep same-value attribute changes and never turn an old carry-in into a fresh sample."""

    async def scenario():
        """Write synthetic original State events through Recorder, then use its actual row-limited API."""
        async with running_chain(
            live=True, recorder=True, fixture_devices=False, peer_gateway=True
        ) as (
            hass,
            entry,
            app_url,
            _,
        ):
            fixture = raw_trace()
            offset = dt_util.utcnow() - instant(fixture["end"]) - timedelta(seconds=2)
            original = {}
            for identity, field in ((ENTITY, "energy_states"), (POWER, "power_states")):
                rows = []
                for point in fixture[field]:
                    at = instant(point["at"]) + offset
                    value, unit = point["state"], point["unit"]
                    if identity == ENTITY:
                        value, unit = str(float(value) * 1000), "Wh"
                    elif value == "3000":
                        value, unit = "3", "kW"
                    state = State(
                        identity,
                        value,
                        {
                            "unit_of_measurement": unit,
                            "device_class": "energy" if identity == ENTITY else "power",
                            "state_class": "total_increasing"
                            if identity == ENTITY
                            else "measurement",
                        },
                        last_updated=at,
                        last_changed=at,
                    )
                    rows.append(state)
                if identity == POWER:
                    at = rows[0].last_updated + timedelta(seconds=100)
                    rows.insert(
                        1,
                        State(
                            identity,
                            "0",
                            {"unit_of_measurement": "kW"},
                            last_updated=at,
                            last_changed=rows[0].last_changed,
                        ),
                    )
                previous = None
                for state in rows:
                    hass.bus.async_fire(
                        EVENT_STATE_CHANGED,
                        {"entity_id": identity, "old_state": previous, "new_state": state},
                    )
                    previous = state
                original[identity] = rows
                hass.states.async_set(
                    identity, rows[-1].state, rows[-1].attributes, force_update=True
                )
            await hass.async_block_till_done()
            await get_instance(hass).async_block_till_done()
            coordinator = SimpleNamespace(
                hass=hass, closed=False, appliance_raw_history_future=None
            )
            body = await appliance_raw_history.capture(coordinator, ENTITY, POWER, True)
            assert body is not None
            actual = body["power_states"]
            assert actual[0]["at"] == original[POWER][0].last_updated.isoformat()
            assert any(row["unit"] == "kW" and row["state"] == "0" for row in actual)
            [cycle] = analyze(body)["cycles"]
            assert cycle["profile"].power_w == (1000, 3000, 1000)
            assert cycle["profile"].energy_kwh == pytest.approx(23 / 45)
            assert not body["limited"]
            from homeassistant.components.energy.data import async_get_manager

            from custom_components.open_energy_planner.catalogue import setup_catalogue

            hass.states.async_set(
                "sensor.raw_grid_power",
                0,
                {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
            )
            hass.states.async_set(
                "sensor.raw_grid_import",
                100,
                {
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                },
            )
            await configure_energy(
                hass,
                entry,
                [
                    {
                        "type": "grid",
                        "stat_energy_from": "sensor.raw_grid_import",
                        "stat_rate": "sensor.raw_grid_power",
                    }
                ],
            )
            manager = await async_get_manager(hass)
            await manager.async_update(
                {"device_consumption": [{"stat_consumption": ENTITY, "stat_rate": POWER}]}
            )
            client = entry.runtime_data.client
            await client.request("POST", "/v1/ha/catalogue", await setup_catalogue(hass))
            consumers = await client.request("GET", "/v1/consumers")
            async with (
                ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session,
                session.put(
                    app_url + "/v1/consumers",
                    json={
                        "expected_version": consumers["version"],
                        "selected": [{"entity_id": ENTITY, "weather_sensitive": False}],
                    },
                ) as saved,
            ):
                assert saved.status == 200
            response = await client.request("POST", "/v1/appliances/raw_history", body)
            [projected] = response["appliances"]["devices"]
            assert projected["profile"]["phase_durations_seconds"] == pytest.approx(
                [600.3, 20, 1179.7]
            )
            assert projected["raw_history"]["status"] == "complete"
            assert "power_curve" not in projected["raw_history"]
            detail = await client.request("GET", "/v1/appliances?raw_entity=" + ENTITY)
            assert len(detail["devices"]) == 1
            assert detail["devices"][0]["entity_id"] == ENTITY
            assert detail["devices"][0]["power_entity"] == POWER
            assert (
                max(
                    row["power_w"] or 0
                    for row in detail["devices"][0]["raw_history"]["power_curve"]
                )
                == 3000
            )
            monkeypatch.setattr(appliance_raw_history, "MAX_RAW_STATES", 3)
            limited = await appliance_raw_history.capture(coordinator, ENTITY, POWER, True)
            assert limited["limited"] is True
            assert len(limited["power_states"]) <= 4
            assert not analyze(limited)["cycles"]

    asyncio.run(scenario())


def test_ha_sensors_preserve_irregular_duration_energy_and_reject_invalid_durations():
    """Native read-only sensor calculations share the app's exact seconds and kWh contract."""
    from custom_components.open_energy_planner.appliances import profile_duration, profile_energy

    value = {"power_w": [1000, 3000, 1000], "phase_durations_seconds": [600.3, 20, 1179.7]}
    assert profile_duration(value) == pytest.approx(1800)
    assert profile_energy(value) == pytest.approx(23 / 45)
    for invalid in ([600.3, -20, 1179.7], [600.3, 20], [600.3, float("nan"), 1179.7]):
        with pytest.raises(ValueError):
            profile_duration({**value, "phase_durations_seconds": invalid})


@pytest.mark.parametrize("cancel", [False, True])
def test_timed_out_or_cancelled_raw_reader_keeps_underlying_recorder_work_admitted_once(
    monkeypatch, cancel
):
    """Awaiter completion cannot queue another native SQL read before the first physical query ends."""

    async def scenario():
        """Block a real Recorder executor call, exercise worker retry, then release and prove later recovery."""
        async with running_chain(recorder=True, fixture_devices=False) as (_, entry, _, _):
            entered, release, calls = Event(), Event(), []
            native = appliance_raw_history.get_last_state_changes

            def blocked(*arguments):
                """Hold the actual native read in its executor rather than merely holding its async awaiter."""
                calls.append(arguments[2])
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("Synthetic blocked Recorder read")
                return native(*arguments)

            monkeypatch.setattr(appliance_raw_history, "get_last_state_changes", blocked)
            monkeypatch.setattr(
                appliance_raw_history, "RAW_QUERY_TIMEOUT_SECONDS", 0.01 if not cancel else 5
            )
            coordinator = entry.runtime_data
            if coordinator.appliance_history_task:
                await coordinator.appliance_history_task
            coordinator.appliance_history_task = None
            reader = asyncio.create_task(
                appliance_raw_history.capture(coordinator, ENTITY, POWER, True)
            )
            assert await asyncio.to_thread(entered.wait, 2)
            if cancel:
                reader.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await reader
            else:
                assert await reader is None
            physical = coordinator.appliance_raw_history_future
            assert not physical.done()

            async def retry(_coordinator):
                """Use the existing cancellable history worker admission for another refresh pass."""
                assert (
                    await appliance_raw_history.capture(_coordinator, ENTITY, POWER, True) is None
                )

            monkeypatch.setattr(appliance_history, "process", retry)
            for _ in range(3):
                await appliance_history.synchronize(coordinator)
                await coordinator.appliance_history_task
            assert calls == [ENTITY]
            if cancel:
                await coordinator.close()
            release.set()
            await asyncio.wait_for(asyncio.shield(physical), 3)
            assert len(calls) == 2
            if cancel:
                assert await appliance_raw_history.capture(coordinator, ENTITY, POWER, True) is None
                assert len(calls) == 2
            else:
                monkeypatch.setattr(appliance_raw_history, "RAW_QUERY_TIMEOUT_SECONDS", 3)
                assert (
                    await appliance_raw_history.capture(coordinator, ENTITY, POWER, True)
                    is not None
                )
                assert len(calls) == 4

    asyncio.run(scenario())
