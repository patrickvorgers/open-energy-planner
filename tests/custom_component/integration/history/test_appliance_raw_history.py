"""Native Recorder row limits, original state semantics and physically retained executor admission."""

import asyncio
import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta
from threading import Event
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant", reason="Native Recorder proof runs in the pinned HA image")

from aiohttp import ClientSession
from homeassistant.components.recorder import get_instance
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import State
from homeassistant.util import dt as dt_util
from sqlalchemy.exc import OperationalError

from custom_components.open_energy_planner import appliance_history, appliance_raw_history
from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.cycles.raw import analyze
from tests.app.appliance.raw_support import dense_raw_trace, raw_captures
from tests.app.appliance.support import ENTITY, POWER
from tests.custom_component.support.chain import TOKEN, configure_energy, running_chain


def test_identical_native_state_reports_refresh_last_reported_without_changing_last_updated():
    """Native HA identical reports remain fresh despite Recorder retaining the old unchanged state time."""

    async def scenario():
        """Use actual HA state reporting and Recorder capture without force_update or invented changed values."""
        async with running_chain(
            live=True, recorder=True, fixture_devices=False, peer_gateway=True
        ) as (hass, _entry, _url, _extra):
            attributes = {
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
            }
            hass.states.async_set(POWER, "383", attributes)
            hass.states.async_set(
                ENTITY,
                "10",
                {
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                },
            )
            first = hass.states.get(POWER)
            first_reported = first.last_reported
            await hass.async_block_till_done()
            hass.states.async_set(POWER, "383", attributes)
            second = hass.states.get(POWER)
            assert second.last_changed == first.last_changed
            assert second.last_updated == first.last_updated
            assert second.last_reported > first_reported
            await hass.async_block_till_done()
            await get_instance(hass).async_block_till_done()
            coordinator = SimpleNamespace(
                hass=hass,
                closed=False,
                appliance_raw_history_future=None,
                appliance_observer_session="native-identical-reports",
            )
            body = await appliance_raw_history.capture(coordinator, ENTITY, POWER, False)
            assert body["observer_session"] == "native-identical-reports"
            assert body["power_reported_at"] == second.last_reported.isoformat()
            assert body["power_states"][-1]["at"] == first.last_updated.isoformat()
            assert body["power_states"][-1]["state"] == "383"

    asyncio.run(scenario())


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
            fixture = dense_raw_trace()
            cycle_start = instant(fixture["end"]) - timedelta(hours=1)
            fixture["power_states"].extend(
                {
                    "at": (cycle_start + timedelta(seconds=index)).isoformat(),
                    "state": "1000",
                    "unit": "W",
                }
                for index in range(1, 143)
            )
            fixture["power_states"].sort(key=lambda row: row["at"])
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
                    at = rows[0].last_updated + timedelta(seconds=100.5)
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
                hass=hass,
                closed=False,
                appliance_raw_history_future=None,
                appliance_observer_session="native-fixture",
            )
            body = await appliance_raw_history.capture(coordinator, ENTITY, POWER, True)
            assert body is not None
            assert 256 * 1024 < len(json.dumps(body).encode()) < 512 * 1024
            actual = body["power_states"]
            assert actual[0]["at"] == original[POWER][0].last_updated.isoformat()
            assert any(row["unit"] == "kW" and row["state"] == "0" for row in actual)
            [cycle] = analyze(body)["cycles"]
            assert len(cycle["profile"].power_w) == 145
            assert max(cycle["profile"].power_w) == 3000
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
            from open_energy_planner.appliances.cycles import api as api_module

            clock = [instant(body["end"])]

            class CaptureClock(datetime):
                """Deliver each genuinely captured fixture at its simulated app admission time."""

                @classmethod
                def now(cls, tz=None):
                    """Advance HTTP admission without waiting for the entire physical cycle."""
                    return clock[0].astimezone(tz)

            monkeypatch.setattr(api_module, "datetime", CaptureClock)
            for frame in raw_captures(body):
                clock[0] = instant(frame["end"])
                await client.request("POST", "/v1/appliances/raw_history", frame)
            response = await client.request("POST", "/v1/appliances/raw_history", body)
            [projected] = response["appliances"]["devices"]
            assert "profile" in projected, {
                key: projected.get(key) for key in ("report_coverage", "raw_history")
            }
            assert len(projected["profile"]["phase_durations_seconds"]) == 145
            assert projected["profile"]["phase_durations_seconds"][-2:] == pytest.approx(
                [20, 1179.7]
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
            from custom_components.open_energy_planner.appliances import receive

            native_coordinator = entry.runtime_data
            native_coordinator.consumer_forecasts = await client.request("GET", "/v1/consumers")
            receive(native_coordinator, await client.request("GET", "/v1/appliances"))
            async with (
                ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session,
                session.post(
                    app_url + "/v1/appliances/device", json={"entity_id": ENTITY, "shiftable": True}
                ) as configured,
            ):
                assert configured.status == 200
            now = dt_util.utcnow()
            await client.request(
                "POST",
                "/v1/appliances/tasks",
                {
                    "entity_id": ENTITY,
                    "request_id": "raw-peer-frozen",
                    "ready_at": now.isoformat(),
                    "deadline": (now + timedelta(hours=4)).isoformat(),
                },
            )
            receive(native_coordinator, await client.request("GET", "/v1/appliances"))
            [received] = native_coordinator.appliances["devices"]
            assert len(received["profile"]["power_w"]) == 145
            assert received["task"]["profile"] == received["profile"]
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


def test_collector_drops_a_row_bounded_frame_above_512_kib(monkeypatch):
    """Even an admitted N+1 native tail must be discarded when its original values exceed the byte budget."""

    async def scenario():
        """Use the native executor lifecycle with maximal synthetic state values and independently measured bytes."""
        async with running_chain(live=True, recorder=True, fixture_devices=False) as (
            hass,
            _,
            _,
            _,
        ):
            first = dt_util.utcnow().replace(microsecond=123456) - timedelta(hours=2)
            originals = {}
            for identity, value, unit in (
                (ENTITY, "10." + "0" * 61, "kWh"),
                (POWER, "0." + "0" * 62, "W"),
            ):
                originals[identity] = [
                    State(
                        identity,
                        value,
                        {"unit_of_measurement": unit},
                        last_updated=first + timedelta(seconds=index),
                    )
                    for index in range(2049)
                ]
            native_bytes = sum(
                len(
                    json.dumps(
                        [
                            {
                                "at": state.last_updated.isoformat(),
                                "state": state.state,
                                "unit": state.attributes["unit_of_measurement"],
                            }
                            for state in rows
                        ]
                    ).encode()
                )
                for rows in originals.values()
            )
            assert native_bytes > 512 * 1024
            calls = []

            def bounded(_hass, count, identity):
                """Return a bounded native tail without changing the production byte admission constant."""
                assert count == 2049
                calls.append(identity)
                return {identity: originals[identity]}

            monkeypatch.setattr(appliance_raw_history, "get_last_state_changes", bounded)
            coordinator = SimpleNamespace(
                hass=hass,
                closed=False,
                appliance_raw_history_future=None,
                appliance_observer_session="native-fixture",
            )
            assert await appliance_raw_history.capture(coordinator, ENTITY, POWER, True) is None
            assert calls == [ENTITY, POWER]
            assert coordinator.appliance_raw_history_future.done()

    asyncio.run(scenario())


def test_recorder_sql_failure_keeps_statistics_scan_and_next_appliance(monkeypatch, caplog):
    """An actual Recorder executor SQL failure stays local to raw capture and never leaks its private values."""
    caplog.set_level(
        logging.DEBUG, logger="custom_components.open_energy_planner.appliance_raw_history"
    )

    async def scenario():
        """Run the real worker and Recorder executor with one failing native raw query among two appliances."""
        async with running_chain(live=True, recorder=True, fixture_devices=False) as (
            hass,
            _,
            _,
            _,
        ):
            other = "sensor.next_energy"
            scans, posts, raw_reads = [], [], []
            native = appliance_raw_history.get_last_state_changes

            def failing(hass, count, identity):
                """Raise the SQL adapter's genuine exception type from inside the physical executor."""
                raw_reads.append(identity)
                if identity == ENTITY:
                    raise OperationalError(
                        "private-sql-sentinel", {}, RuntimeError("private-driver-sentinel")
                    )
                return native(hass, count, identity)

            async def request(method, path, body=None):
                """Supply selected source ownership and retain each real worker upload for fairness assertions."""
                if path.startswith("/v1/consumers"):
                    return {
                        "options": [
                            {"entity_id": entity, "power_entity": POWER}
                            for entity in (ENTITY, other)
                        ],
                        "selected": [{"entity_id": entity} for entity in (ENTITY, other)],
                    }
                if path == "/v1/appliances":
                    return {"devices": []}
                posts.append((path, body["entity_id"]))
                return {}

            async def historical(_coordinator, _device, entity, *_arguments):
                """Observe that raw degradation cannot bypass the existing bounded statistical scan owner."""
                scans.append(entity)
                return False

            monkeypatch.setattr(appliance_raw_history, "get_last_state_changes", failing)
            monkeypatch.setattr(appliance_history, "historical", historical)
            coordinator = SimpleNamespace(
                hass=hass,
                closed=False,
                client=SimpleNamespace(request=request),
                appliance_history_attempts={},
                appliance_raw_history_future=None,
                appliance_observer_session="native-fixture",
            )
            await appliance_history.process(coordinator)
            assert scans == [ENTITY, other]
            assert raw_reads == [ENTITY, other, POWER]
            assert posts.count(("/v1/appliances/history", ENTITY)) == 2
            assert posts.count(("/v1/appliances/history", other)) == 2
            assert "private-sql-sentinel" not in caplog.text
            assert "private-driver-sentinel" not in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["learned", "frozen"])
@pytest.mark.parametrize(
    "count,irregular,accepted",
    [(145, True, True), (2048, True, True), (2049, True, False), (145, False, False)],
)
def test_native_receiver_applies_profile_specific_phase_limits(target, count, irregular, accepted):
    """Both learned and accepted task profiles admit irregular traces while retaining legacy and per-profile bounds."""
    from custom_components.open_energy_planner.appliances import profile_energy, receive

    async def scenario():
        """Exercise the actual native receiver and verify failed admission is atomic."""
        async with running_chain(fixture_devices=False) as (hass, entry, _, _):
            value = {"power_w": [1000] * count}
            if irregular:
                value["phase_durations_seconds"] = [1] * (count - 1) + [max(1, 1801 - count)]
            row = {"entity_id": ENTITY, "shiftable": True}
            if target == "learned":
                row["profile"] = value
            else:
                row["task"] = {
                    "id": "accepted",
                    "status": "pending",
                    "deadline": (dt_util.utcnow() + timedelta(hours=4)).isoformat(),
                    "profile": value,
                    "attempt": None,
                }
            coordinator = SimpleNamespace(
                hass=hass,
                entry=entry,
                consumer_forecasts={"options": [{"entity_id": ENTITY}]},
                appliances={},
            )
            profile_issue = (
                "Invalid average cycle profile"
                if target == "learned"
                else "Invalid accepted cycle profile"
            )
            if accepted:
                receive(coordinator, {"devices": [row]})
                assert coordinator.appliances["devices"] == [row]
                assert profile_energy(value) == pytest.approx(0.5 if count == 145 else 128 / 225)
                previous = deepcopy(coordinator.appliances)
                for bad, issue in (
                    (-1, profile_issue),
                    (float("nan"), None),
                    (True, profile_issue),
                ):
                    invalid = deepcopy(row)
                    shape = (
                        invalid["profile"] if target == "learned" else invalid["task"]["profile"]
                    )
                    shape["power_w"][0] = bad
                    with pytest.raises(ValueError, match=issue):
                        receive(coordinator, {"devices": [invalid]})
                    assert coordinator.appliances == previous
            else:
                with pytest.raises(ValueError, match=profile_issue):
                    receive(coordinator, {"devices": [row]})
                assert coordinator.appliances == {}

    asyncio.run(scenario())


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
