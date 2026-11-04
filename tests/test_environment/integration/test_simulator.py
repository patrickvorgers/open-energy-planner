"""Verify simulator initialization, fixture repair arithmetic and retained history."""

import asyncio
from datetime import timedelta

import pytest

pytest.importorskip(
    "homeassistant", reason="Real HA integration suite runs in the pinned container"
)


from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from tests.custom_component.support.chain import running_chain, setup_energy


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_simulated_ev_rejects_nonfinite_service_values(value):
    """Nonfinite power or SoC must fail before a durable simulated counter can be poisoned."""
    import voluptuous as vol

    from test_environment.oep_test_fixture.ev import finite_number

    with pytest.raises(vol.Invalid, match="finite"):
        finite_number(value)


def test_seeded_lab_without_schema_marker_preserves_energy_configuration(monkeypatch):
    """An already-seeded lab preserves Energy membership and counter history while joining imported sums to the native origin."""
    from copy import deepcopy
    from unittest.mock import Mock

    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.storage import Store

    from test_environment import oep_test_fixture

    async def scenario():
        """Use a real HA store/manager and observe normalized statistics imports without replacing configured devices."""
        async with running_chain(live=True, recorder=True) as (hass, entry, _url, _root):
            manager = await async_get_manager(hass)
            sources = [
                {
                    "type": "grid",
                    "stat_energy_from": "sensor.custom_import",
                    "stat_rate": "sensor.custom_power",
                }
            ]
            devices = [{"stat_consumption": "sensor.custom_device"}]
            await manager.async_update({"energy_sources": sources, "device_consumption": devices})
            original = deepcopy(manager.data)
            anchor = (
                (dt_util.utcnow() - timedelta(days=1))
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .isoformat()
            )
            store = Store(hass, 1, "oep_test_fixture")
            await store.async_save({"anchor": anchor, "seeded": True})
            writes = []
            original_save = store.async_save

            async def retain_seed_marker(data):
                """Every intermediate write must retain hourly completion if fine-history import is interrupted."""
                writes.append(deepcopy(data))
                await original_save(data)

            monkeypatch.setattr(store, "async_save", retain_seed_marker)
            monkeypatch.setattr(
                "test_environment.oep_test_fixture.runtime.Store", Mock(return_value=store)
            )
            from test_environment.oep_test_fixture.history import async_import_statistics

            imports = Mock(wraps=async_import_statistics)
            monkeypatch.setattr(
                "test_environment.oep_test_fixture.history.async_import_statistics", imports
            )
            assert await oep_test_fixture.async_setup_entry(hass, entry)
            await hass.async_block_till_done(wait_background_tasks=True)
            await get_instance(hass).async_block_till_done()
            assert manager.data["energy_sources"] == original["energy_sources"]
            assert all(item in manager.data["device_consumption"] for item in devices)
            added_consumers = [
                item for item in manager.data["device_consumption"] if item not in devices
            ]
            assert len(added_consumers) == 7
            assert {item["stat_consumption"] for item in added_consumers} == {
                "sensor.oep_test_heat_pump_energy",
                "sensor.oep_test_dishwasher_energy",
                "sensor.oep_test_washing_machine_energy",
                "sensor.oep_test_dryer_energy",
                "sensor.oep_test_wallbox_a_energy",
                "sensor.oep_test_wallbox_b_energy",
                "sensor.oep_test_boiler_energy",
            }
            assert await store.async_load() == {
                "anchor": anchor,
                "seeded": True,
                "energy_schema": 1,
                "quarter_history_version": 3,
            }
            assert imports.call_count == len(oep_test_fixture.ENERGY) + 4
            assert all(value["seeded"] for value in writes)
            fine_start = (dt_util.utcnow() - timedelta(hours=2)).replace(
                minute=0, second=0, microsecond=0
            )
            ids = [
                er.async_get(hass).async_get_entity_id("sensor", "oep_test_fixture", key)
                for key in oep_test_fixture.ENERGY
            ]
            assert all(ids)
            fine = await get_instance(hass).async_add_executor_job(
                statistics_during_period,
                hass,
                fine_start,
                fine_start + timedelta(hours=1),
                ids,
                "5minute",
                {"energy": "kWh"},
                {"sum"},
            )
            assert set(fine) == set(ids)
            assert all(len(values) == 12 for values in fine.values())

    asyncio.run(scenario())


@pytest.mark.parametrize("power_config", [False, True], ids=["flat-power", "nested-power"])
def test_native_test_data_reuses_energy_ids_units_and_preferences(power_config):
    """Each fresh Energy configuration preserves history and units through both absent and restored sensor reloads."""
    from copy import deepcopy
    from functools import partial

    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period

    from test_environment.oep_test_fixture.existing import async_setup_existing

    async def scenario():
        """Use actual Recorder imports and read-back so test-only state updates cannot masquerade as stored history."""
        async with running_chain(live=True, recorder=True) as (hass, entry, _url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await setup_energy(hass)
            manager = await async_get_manager(hass)
            sources = [
                {
                    "type": "grid",
                    "stat_energy_from": "sensor.native_import",
                    "stat_energy_to": "sensor.native_export",
                    "stat_rate": "sensor.native_power",
                },
                {"type": "solar", "stat_energy_from": "sensor.native_solar"},
            ]
            if power_config:
                sources[0]["power_config"] = {"stat_rate": sources[0].pop("stat_rate")}
            await manager.async_update({"energy_sources": sources, "device_consumption": []})
            sources = deepcopy(manager.data["energy_sources"])
            ids = {"sensor.native_import", "sensor.native_export", "sensor.native_solar"}
            hass.states.async_set(
                "sensor.native_power",
                "99999",
                {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
            )
            if power_config:
                hass.states.async_remove("sensor.native_power")
            for entity in ids:
                hass.states.async_set(
                    entity,
                    "1000000",
                    {
                        "unit_of_measurement": "Wh" if entity.endswith("solar") else "kWh",
                        "device_class": "energy",
                        "state_class": "total_increasing",
                    },
                )
            assert await async_setup_existing(hass, entry)
            extended = manager.data["energy_sources"]
            assert extended[: len(sources)] == sources
            assert sum(source["type"] == "solar" for source in extended) == 2
            assert sum(source["type"] == "battery" for source in extended) == 1
            incomplete = deepcopy(extended)
            battery = next(source for source in incomplete if source["type"] == "battery")
            battery.pop("stat_soc")
            battery.pop("capacity")
            await manager.async_update({"energy_sources": incomplete})
            before_refresh = manager.data["energy_sources"]
            from test_environment.oep_test_fixture.existing import extend_installation

            await extend_installation(hass, entry, manager)
            assert "capacity" not in next(
                source for source in before_refresh if source["type"] == "battery"
            )
            extended = manager.data["energy_sources"]
            assert (
                next(source for source in extended if source["type"] == "battery")["capacity"] == 10
            )
            ids |= {
                source[key]
                for source in extended
                for key in ("stat_energy_from", "stat_energy_to")
                if source.get(key)
            }
            from custom_components.open_energy_planner.catalogue import energy_suggestions

            hints = await energy_suggestions(hass)
            assert hints["batteries"][0]["soc_entity"] == "sensor.oep_test_battery_soc"
            assert hints["batteries"][0]["power_entity"] == "sensor.oep_test_battery_power"
            assert abs(float(hass.states.get("sensor.native_power").state)) <= 3000
            assert hass.states.get("sensor.native_power").attributes["unit_of_measurement"] == "W"
            await hass.async_block_till_done(wait_background_tasks=True)
            await get_instance(hass).async_block_till_done()
            metadata = await get_instance(hass).async_add_executor_job(
                partial(get_metadata, hass, statistic_ids=ids)
            )
            assert set(metadata) == ids
            assert metadata["sensor.native_solar"][1]["unit_of_measurement"] == "Wh"
            start = dt_util.utcnow() - timedelta(days=31)
            # Recorder can finish the latest hour while live fixture counters keep advancing.
            history_end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(
                hours=1
            )
            first = await get_instance(hass).async_add_executor_job(
                statistics_during_period,
                hass,
                start,
                history_end,
                ids,
                "hour",
                {"energy": "kWh"},
                {"sum"},
            )
            assert all(len(rows) >= 30 * 24 for rows in first.values())
            consumer_id = next(
                item["stat_consumption"]
                for item in manager.data["device_consumption"]
                if item["stat_consumption"].startswith("sensor.oep_test_heat_pump")
            )
            consumer_history = await get_instance(hass).async_add_executor_job(
                statistics_during_period,
                hass,
                start,
                None,
                {consumer_id},
                "hour",
                {"energy": "kWh"},
                {"sum"},
            )
            assert len(consumer_history[consumer_id]) >= 30 * 24

            fine_start = (dt_util.utcnow() - timedelta(days=1)).replace(
                minute=0, second=0, microsecond=0
            )
            fine = await get_instance(hass).async_add_executor_job(
                statistics_during_period,
                hass,
                fine_start,
                fine_start + timedelta(hours=1),
                ids,
                "5minute",
                {"energy": "kWh"},
                {"sum"},
            )
            assert all(len(values) == 12 for values in fine.values())
            for entity, values in fine.items():
                hour = next(row for row in first[entity] if row["start"] == fine_start.timestamp())
                assert values[-1]["sum"] == pytest.approx(hour["sum"])
            # Both terminal sensor conditions share only the already accepted history.
            # Reset their states and capture fresh counters for each real reload.
            for restored_placeholders in (False, True):
                counters = {entity: float(hass.states.get(entity).state) for entity in ids}
                events = []

                @callback
                def record_counter(event, expected_counters=counters, observed_events=events):
                    """Record numeric counter changes across reload.

                    Ignore unknown and unavailable state-change events.
                    """
                    state = event.data.get("new_state")
                    if (
                        state
                        and state.entity_id in expected_counters
                        and state.state not in ("unknown", "unavailable")
                    ):
                        observed_events.append((state.entity_id, float(state.state)))

                cancel = hass.bus.async_listen("state_changed", record_counter)
                assert await async_setup_existing(hass, entry)
                await hass.async_block_till_done(wait_background_tasks=True)
                await get_instance(hass).async_block_till_done()
                second = await get_instance(hass).async_add_executor_job(
                    statistics_during_period,
                    hass,
                    start,
                    history_end,
                    ids,
                    "hour",
                    {"energy": "kWh"},
                    {"sum"},
                )
                assert first == second
                assert all(value >= counters[entity] for entity, value in events)
                counters = {entity: float(hass.states.get(entity).state) for entity in ids}
                events.clear()
                for entity in (
                    "sensor.oep_test_solar_garage",
                    "sensor.oep_test_battery_charge",
                    "sensor.oep_test_battery_discharge",
                ):
                    if restored_placeholders:
                        hass.states.async_set(entity, "unavailable", {})
                    else:
                        hass.states.async_remove(entity)
                assert await async_setup_existing(hass, entry)
                await hass.async_block_till_done(wait_background_tasks=True)
                await get_instance(hass).async_block_till_done()
                restored_history = await get_instance(hass).async_add_executor_job(
                    statistics_during_period,
                    hass,
                    start,
                    history_end,
                    ids,
                    "hour",
                    {"energy": "kWh"},
                    {"sum"},
                )
                assert restored_history == first, f"{restored_placeholders=}"
                assert all(
                    float(hass.states.get(entity).state) >= value
                    for entity, value in counters.items()
                )
                assert all(value >= counters[entity] for entity, value in events)
                cancel()
                assert manager.data["energy_sources"] == extended
                for entity in (
                    "sensor.oep_test_battery_charge",
                    "sensor.oep_test_battery_discharge",
                    "sensor.oep_test_solar_garage",
                ):
                    assert first[entity][-1]["sum"] > first[entity][0]["sum"]
                assert 50 <= float(hass.states.get("sensor.oep_test_battery_soc").state) <= 62
            # Existing fixture timers restore native_import. Use an external meter they do not own.
            missing_units = deepcopy(extended)
            next(source for source in missing_units if source["type"] == "grid")[
                "stat_energy_from"
            ] = "sensor.external_missing_units"
            await manager.async_update({"energy_sources": missing_units})
            hass.states.async_set("sensor.external_missing_units", "unavailable", {})
            with pytest.raises(ValueError, match="original energy units"):
                await async_setup_existing(hass, entry)
            await manager.async_update({"energy_sources": extended})
            from types import SimpleNamespace

            from homeassistant.helpers.storage import Store

            from test_environment.oep_test_fixture import async_remove_entry

            assert await Store(hass, 1, "oep_existing_energy_test").async_load()
            await async_remove_entry(hass, SimpleNamespace(data={"existing_energy": True}))
            assert await Store(hass, 1, "oep_existing_energy_test").async_load() is None
            assert manager.data["energy_sources"] == extended

    asyncio.run(scenario())


@pytest.mark.parametrize("yaml_configured", [False, True])
def test_fixture_user_setup_cannot_start_the_yaml_scenario(yaml_configured):
    """Only explicit lab YAML may create the default scenario. Native UI setup must never replace existing Energy sources."""
    from unittest.mock import AsyncMock, patch

    from test_environment.oep_test_fixture import async_setup

    async def scenario():
        """Observe the genuine HA task scheduling boundary used when a user adds a custom integration."""
        async with running_chain(live=True) as (hass, _entry, _url, _root):
            with patch.object(
                hass.config_entries.flow, "async_init", new_callable=AsyncMock
            ) as start:
                assert await async_setup(hass, {"oep_test_fixture": {}} if yaml_configured else {})
                await hass.async_block_till_done()
                assert start.await_count == int(yaml_configured)
                if yaml_configured:
                    assert start.call_args.kwargs["context"] == {"source": "import"}

    asyncio.run(scenario())


def test_existing_lab_initializes_new_ev_history_after_ha_start_without_bootstrap_deadlock():
    """A previously seeded lab must return from integration setup before Recorder starts, then add new EVs without resetting its retained seed."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.helpers.storage import Store

    from test_environment import oep_test_fixture

    async def scenario():
        """Pre-start setup creates no EVs. Startup creates both car SoC sensors and wallbox energy while preserving the existing seed."""
        original = {
            "anchor": (dt_util.utcnow() - timedelta(days=32))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat(),
            "seeded": True,
            "energy_schema": 1,
            "quarter_history_version": 3,
        }

        async def prepare(hass, entry, _url):
            """Retain an existing lab seed and require nonblocking setup while HA is not running."""
            assert not hass.is_running
            from homeassistant.auth import auth_manager_from_config
            from homeassistant.components.energy.data import async_get_manager

            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await setup_energy(hass)
            manager = await async_get_manager(hass)
            await manager.async_update(
                {
                    "energy_sources": [
                        {
                            "type": "grid",
                            "stat_energy_from": "sensor.oep_test_grid_import",
                            "stat_energy_to": "sensor.oep_test_grid_export",
                            "stat_rate": "sensor.oep_test_grid_power",
                        }
                    ],
                    "device_consumption": [],
                }
            )
            await Store(hass, 1, "oep_test_fixture").async_save(original)
            assert await asyncio.wait_for(oep_test_fixture.async_setup_entry(hass, entry), 1)
            assert hass.states.get("sensor.oep_test_car_a_soc") is None

        async with running_chain(live=True, recorder=True, before_start=prepare) as (
            hass,
            _entry,
            _url,
            _root,
        ):
            await hass.async_block_till_done(wait_background_tasks=True)
            await get_instance(hass).async_block_till_done()
            assert hass.states.get("sensor.oep_test_car_a_soc").state == "35"
            assert hass.states.get("sensor.oep_test_car_b_soc").state == "45"
            assert hass.states.get("sensor.oep_test_wallbox_a_energy") is not None
            assert await Store(hass, 1, "oep_test_fixture").async_load() == original

    asyncio.run(scenario())


def test_fresh_lab_seeds_household_before_individual_consumer():
    """A fresh simulator creates its Energy sources first, then derives nonnegative disjoint heat-pump history."""
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    from test_environment import oep_test_fixture

    async def scenario():
        """Exercise the actual initialization sequence without pre-existing Energy configuration."""
        async with running_chain(live=True, recorder=True) as (hass, entry, _url, _root):
            manager = await async_get_manager(hass)
            await manager.async_update({"energy_sources": [], "device_consumption": []})
            assert await oep_test_fixture.async_setup_entry(hass, entry)
            await hass.async_block_till_done(wait_background_tasks=True)
            await get_instance(hass).async_block_till_done()
            assert manager.data["energy_sources"]
            consumer = manager.data["device_consumption"][0]["stat_consumption"]
            end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
            rows = await get_instance(hass).async_add_executor_job(
                statistics_during_period,
                hass,
                end - timedelta(days=2),
                end,
                {consumer},
                "hour",
                {"energy": "kWh"},
                {"sum"},
            )
            assert len(rows[consumer]) >= 24
            amounts = [
                after["sum"] - before["sum"]
                for before, after in zip(rows[consumer], rows[consumer][1:])
            ]
            assert min(amounts) >= 0 and max(amounts) > 0

    asyncio.run(scenario())


@pytest.mark.parametrize("origin", ["short_term", "hourly", "new_meter"])
def test_simulator_history_keeps_native_sum_origin_when_reseeding(origin):
    """Persisted fine/hourly origin 1500.0 joins seeded 1500.3 at sum .3. A new meter ends at zero without inventing energy."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.db_schema import StatisticsShortTerm
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import (
        async_import_statistics,
        statistics_during_period,
    )

    from test_environment.oep_test_fixture import history

    async def scenario():
        """Read persisted Recorder origins without mocks. Retain .1-kWh increments and source rows."""
        async with running_chain(recorder=True) as (hass, _entry, _url, _root):
            start = (dt_util.utcnow() - timedelta(hours=2)).replace(
                minute=0, second=0, microsecond=0
            )
            metadata = {
                "statistic_id": "sensor.fixture_join",
                "source": "recorder",
                "name": "Fixture join",
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
                "unit_of_measurement": "kWh",
                "unit_class": "energy",
            }
            if origin != "new_meter":
                previous = [{"start": start - timedelta(hours=1), "sum": 0.0, "state": 1500.0}]
                if origin == "short_term":
                    get_instance(hass).async_import_statistics(
                        metadata, previous, StatisticsShortTerm
                    )
                else:
                    async_import_statistics(hass, metadata, previous)
                # Queue a fence even when Recorder already dequeued the active import.
                await hass.async_add_executor_job(get_instance(hass).block_till_done)
            rows = [
                {
                    "start": start + timedelta(minutes=5 * index),
                    "state": 1500.1 + 0.1 * index,
                    "sum": 1500.1 + 0.1 * index,
                }
                for index in range(3)
            ]
            await history.import_fine(
                hass, metadata, rows, [{"start": start, "sum": 1500.3, "state": 1500.3}]
            )
            await hass.async_add_executor_job(get_instance(hass).block_till_done)
            result = await get_instance(hass).async_add_executor_job(
                statistics_during_period,
                hass,
                start,
                start + timedelta(minutes=15),
                {"sensor.fixture_join"},
                "5minute",
                {"energy": "kWh"},
                {"sum", "state"},
            )
            assert [row["sum"] for row in result["sensor.fixture_join"]] == pytest.approx(
                [-0.2, -0.1, 0] if origin == "new_meter" else [0.1, 0.2, 0.3]
            )
            assert [row["state"] for row in result["sensor.fixture_join"]] == pytest.approx(
                [1500.1, 1500.2, 1500.3]
            )
            assert [row["sum"] for row in rows] == pytest.approx([1500.1, 1500.2, 1500.3])
            hourly = await get_instance(hass).async_add_executor_job(
                statistics_during_period,
                hass,
                start,
                start + timedelta(hours=1),
                {"sensor.fixture_join"},
                "hour",
                {"energy": "kWh"},
                {"sum", "state"},
            )
            assert hourly["sensor.fixture_join"][0]["sum"] == pytest.approx(
                0 if origin == "new_meter" else 0.3
            )
            assert hourly["sensor.fixture_join"][0]["state"] == pytest.approx(1500.3)

    asyncio.run(scenario())


def test_simulator_history_barrier_waits_for_a_dequeued_import():
    """A queued barrier waits for an active import even after Recorder's queue empties."""
    from threading import Event
    from unittest.mock import patch

    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.db_schema import StatisticsShortTerm
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import get_last_short_term_statistics
    from homeassistant.components.recorder.tasks import ImportStatisticsTask, WaitTask

    async def scenario():
        """Pause a real import, observe its queued fence, then verify durable meter data."""
        async with running_chain(recorder=True) as (hass, _entry, _url, _root):
            recorder = get_instance(hass)
            started, fenced = asyncio.Event(), asyncio.Event()
            release = Event()
            original_import, original_queue = ImportStatisticsTask.run, recorder.queue_task

            def pause_import(task, instance):
                """Hold the dequeued import until the test observes its waiting fence."""
                hass.loop.call_soon_threadsafe(started.set)
                if not release.wait(10):
                    raise TimeoutError("The synthetic Recorder import was not released")
                original_import(task, instance)

            def track_fence(task):
                """Expose the fence enqueue without changing Recorder's task order."""
                original_queue(task)
                if isinstance(task, WaitTask):
                    hass.loop.call_soon_threadsafe(fenced.set)

            metadata = {
                "statistic_id": "sensor.fixture_barrier",
                "source": "recorder",
                "name": "Fixture barrier",
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
                "unit_of_measurement": "kWh",
                "unit_class": "energy",
            }
            previous = [
                {
                    "start": (dt_util.utcnow() - timedelta(hours=3)).replace(
                        minute=0, second=0, microsecond=0
                    ),
                    "sum": 0.0,
                    "state": 1500.0,
                }
            ]
            with (
                patch.object(ImportStatisticsTask, "run", pause_import),
                patch.object(recorder, "queue_task", track_fence),
            ):
                waiter = None
                try:
                    recorder.async_import_statistics(metadata, previous, StatisticsShortTerm)
                    await asyncio.wait_for(started.wait(), 5)
                    assert recorder.async_get_commit_future() is None
                    waiter = hass.async_add_executor_job(recorder.block_till_done)
                    await asyncio.wait_for(fenced.wait(), 5)
                    assert not waiter.done()
                finally:
                    release.set()
                    if waiter is not None:
                        await asyncio.wait_for(waiter, 10)
            result = await recorder.async_add_executor_job(
                get_last_short_term_statistics,
                hass,
                1,
                "sensor.fixture_barrier",
                False,
                {"sum", "state"},
            )
            assert result["sensor.fixture_barrier"][0]["sum"] == 0.0
            assert result["sensor.fixture_barrier"][0]["state"] == 1500.0

    asyncio.run(scenario())


def test_laundry_fixtures_keep_independent_cycles_and_preserve_energy_membership():
    """Washer .92 kWh over 150 minutes and dryer 1.4 kWh over 180 minutes preserve separate durable meters and existing consumers."""
    from unittest.mock import patch

    from homeassistant.components.energy.data import async_get_manager

    from test_environment.oep_test_fixture.dishwasher import laundry_extra, setup_laundry

    async def scenario():
        """Run only synthetic start/advance services against the isolated HA Recorder."""
        async with running_chain(live=True, recorder=True) as (hass, entry, _url, _root):
            manager = await async_get_manager(hass)
            original = list((manager.data or {}).get("device_consumption", []))
            with patch(
                "test_environment.oep_test_fixture.dishwasher.rows",
                side_effect=AssertionError("Laundry must not generate historical measurements"),
            ):
                await setup_laundry(hass, entry)
            for identity, expected_kwh, seconds in (
                ("washing_machine", 0.92, 9000),
                ("dryer", 1.4, 10800),
            ):
                entity = "sensor.oep_test_" + identity + "_energy"
                state = hass.states.get(entity)
                assert float(state.state) == 50.0
                assert state.attributes["synthetic"] is True
                await hass.services.async_call(
                    "oep_test_fixture", identity + "_start", {}, blocking=True
                )
                await hass.services.async_call(
                    "oep_test_fixture", identity + "_advance", {"seconds": seconds}, blocking=True
                )
                assert float(hass.states.get(entity).state) == pytest.approx(50 + expected_kwh)
                assert (
                    hass.states.get("sensor.oep_test_" + identity + "_status").state == "completed"
                )
            totals, power = laundry_extra(hass)
            assert sum(total - initial for total, initial in totals.values()) == pytest.approx(2.32)
            assert power == 0
            with patch(
                "test_environment.oep_test_fixture.dishwasher.rows",
                side_effect=AssertionError("Laundry must not generate historical measurements"),
            ):
                await setup_laundry(hass, entry)
            consumers = (manager.data or {})["device_consumption"]
            assert all(item in consumers for item in original)
            assert (
                sum(
                    item["stat_consumption"] == "sensor.oep_test_washing_machine_energy"
                    for item in consumers
                )
                == 1
            )
            assert (
                sum(
                    item["stat_consumption"] == "sensor.oep_test_dryer_energy" for item in consumers
                )
                == 1
            )
            await manager.async_update(
                {
                    "device_consumption": [
                        {key: value for key, value in item.items() if key != "stat_rate"}
                        if item["stat_consumption"] == "sensor.oep_test_washing_machine_energy"
                        else item
                        for item in consumers
                    ]
                }
            )
            hass.data.pop("oep_test_washing_machine")
            await setup_laundry(hass, entry)
            retained = next(
                item
                for item in manager.data["device_consumption"]
                if item["stat_consumption"] == "sensor.oep_test_washing_machine_energy"
            )
            assert "stat_rate" not in retained
            assert float(
                hass.states.get("sensor.oep_test_washing_machine_energy").state
            ) == pytest.approx(50.92)

    asyncio.run(scenario())
