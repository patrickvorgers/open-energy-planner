"""Verify native Recorder balance repair, preferences, both statistics resolutions, recovery journaling and later hourly compilation."""

import asyncio
from datetime import timedelta

import pytest

pytest.importorskip(
    "homeassistant", reason="Real HA integration suite runs in the pinned container"
)


from homeassistant.util import dt as dt_util

from tests.custom_component.support.chain import (
    running_chain,
)


@pytest.mark.parametrize("origin_reset", [False, True])
@pytest.mark.parametrize("source_spike", [False, True])
def test_native_balance_preview_apply_keeps_preferences_and_both_statistics_resolutions(
    monkeypatch,
    origin_reset,
    source_spike,
):
    """Repair .83-kWh device use with optional grid-origin drops and a Wh-source PV spike, retaining preferences.

    A foreign source stops the action before statistics access. Restoring the
    declared sources permits .660833 import and 1.830833 export without negative
    fine deltas. Native unit conversion removes a PV spike without extra energy.
    """
    from copy import deepcopy
    from itertools import pairwise

    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.db_schema import StatisticsShortTerm
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import (
        async_import_statistics,
        statistics_during_period,
    )
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.storage import Store

    from test_environment.oep_test_fixture.balance import declare_sources, setup_balance
    from test_environment.oep_test_fixture.dishwasher import DATA_KEY as DISHWASHER_KEY
    from test_environment.oep_test_fixture.ev import DATA_KEY as EV_KEY

    async def scenario():
        """Reject foreign sources before statistics access, then repair declared channels through native Recorder.

        Restoring Energy preferences re-enables the preview. Verify both source
        resolutions, the recovery journal and subsequent native hourly compilation.
        """
        async with running_chain(live=True, recorder=True) as (hass, entry, _url, _root):
            registry = er.async_get(hass)
            consumers = []
            for unique in ("dishwasher_energy", "forecast_heat_pump_energy"):
                consumers.append(
                    registry.async_get_or_create(
                        "sensor", "oep_test_fixture", unique, config_entry=entry
                    ).entity_id
                )
            dishwasher, heat = consumers
            manager = await async_get_manager(hass)
            await manager.async_update(
                {
                    "energy_sources": [
                        {
                            "type": "grid",
                            "stat_energy_from": "sensor.test_import",
                            "stat_energy_to": "sensor.test_export",
                        },
                        {"type": "solar", "stat_energy_from": "sensor.test_pv"},
                    ],
                    "device_consumption": [{"stat_consumption": entity} for entity in consumers],
                }
            )
            before = deepcopy(manager.data)
            start = (dt_util.utcnow() - timedelta(days=1)).replace(
                hour=12, minute=0, second=0, microsecond=0
            )
            recorder = get_instance(hass)
            for entity, amount in {
                "sensor.test_import": 0,
                "sensor.test_export": 2,
                "sensor.test_pv": 2.2,
                dishwasher: 0.83,
                heat: 0.04,
            }.items():
                metadata = {
                    "statistic_id": entity,
                    "name": entity,
                    "source": "recorder",
                    "has_sum": True,
                    "mean_type": StatisticMeanType.NONE,
                    "unit_of_measurement": "Wh"
                    if entity in ("sensor.test_pv", "sensor.test_import")
                    else "MWh"
                    if entity == "sensor.test_export"
                    else "kWh",
                    "unit_class": "energy",
                }
                hourly = [
                    {
                        "start": start + timedelta(hours=index),
                        "sum": 100 + (amount if index else 0),
                        "state": 100 + (amount if index else 0),
                    }
                    for index in range(3)
                ]
                factor = {"Wh": 1000, "MWh": 0.001, "kWh": 1}[metadata["unit_of_measurement"]]
                for row in hourly:
                    row["sum"] *= factor
                    row["state"] *= factor
                    if (
                        origin_reset
                        and entity in ("sensor.test_import", "sensor.test_export")
                        and row["start"] > start
                    ):
                        row["sum"] -= 50 * factor
                async_import_statistics(hass, metadata, hourly)
                fine = []
                for part in range(14):
                    energy = (
                        (amount if part else 0)
                        if entity == dishwasher
                        else amount * min(part, 12) / 12
                    )
                    fine.append(
                        {
                            "start": start + timedelta(minutes=55 + part * 5),
                            "sum": (500 + energy) * factor,
                            "state": (100 + energy) * factor,
                        }
                    )
                    if (
                        origin_reset
                        and part
                        and entity in ("sensor.test_import", "sensor.test_export")
                    ):
                        fine[-1]["sum"] -= 50 * factor
                    if source_spike and entity == "sensor.test_pv" and part == 5:
                        fine[-1]["sum"] += 375 * factor
                        fine[-1]["state"] += 375 * factor
                recorder.async_import_statistics(metadata, fine, StatisticsShortTerm)
            await recorder.async_block_till_done()
            hass.data[DISHWASHER_KEY] = {"ids": {"energy": dishwasher}}
            hass.data[EV_KEY] = {"points": []}
            declare_sources(
                hass,
                entry,
                {
                    "sensor.test_import": "grid_import",
                    "sensor.test_export": "grid_export",
                    "sensor.test_pv": "solar",
                },
                lambda _: {"sensor.test_pv": 0.55},
            )
            await setup_balance(hass, entry)
            await manager.async_update(
                {
                    "energy_sources": [
                        *before["energy_sources"],
                        {"type": "solar", "stat_energy_from": "sensor.foreign_pv"},
                    ]
                }
            )
            with pytest.raises(ValueError, match="actively simulated channels"):
                await hass.services.async_call(
                    "oep_test_fixture",
                    "repair_energy_balance",
                    {},
                    blocking=True,
                    return_response=True,
                )
            await manager.async_update(before)
            if origin_reset:
                from test_environment.oep_test_fixture.consumer import setup_consumer

                await setup_consumer(hass, entry)
                before["device_consumption"].extend(
                    {"stat_consumption": energy_entity, "stat_rate": power_entity}
                    for energy_entity, power_entity in (
                        (
                            "sensor.oep_test_washing_machine_energy",
                            "sensor.oep_test_washing_machine_power",
                        ),
                        ("sensor.oep_test_dryer_energy", "sensor.oep_test_dryer_power"),
                    )
                )
                before["device_consumption"].append(
                    {"stat_consumption": "sensor.oep_test_boiler_energy"}
                )
                assert manager.data == before
                retained = await recorder.async_add_executor_job(
                    statistics_during_period,
                    hass,
                    start,
                    None,
                    {heat},
                    "hour",
                    {"energy": "kWh"},
                    {"sum"},
                )
                assert [row["sum"] for row in retained[heat]] == pytest.approx(
                    [100, 100.04, 100.04]
                )
            from homeassistant.auth import auth_manager_from_config
            from homeassistant.auth.const import GROUP_ID_USER

            hass.auth = await auth_manager_from_config(hass, [], [])
            await hass.auth.async_create_user("Owner")
            reader = await hass.auth.async_create_user("Reader", group_ids=[GROUP_ID_USER])
            assert not reader.is_admin
            with pytest.raises(Unauthorized):
                await hass.services.async_call(
                    "oep_test_fixture",
                    "repair_energy_balance",
                    {},
                    blocking=True,
                    return_response=True,
                    context=Context(user_id=reader.id),
                )
            preview = await hass.services.async_call(
                "oep_test_fixture", "repair_energy_balance", {}, blocking=True, return_response=True
            )
            assert sum("adjustments" in row for row in preview["repairs"]) == 1
            assert (
                any(
                    row["reason"] == "simulated_source_origin_discontinuity"
                    and row["expected_kwh"] == pytest.approx(2.2)
                    for row in preview["repairs"]
                )
                == source_spike
            )
            assert preview["applied"] is False
            assert preview["repairs"][0]["adjustments"]["sensor.test_export"] == pytest.approx(
                (50 if origin_reset else 0) - 0.1691666666666667
            )
            assert preview["repairs"][0]["adjustments"]["sensor.test_import"] == pytest.approx(
                (50 if origin_reset else 0) + 0.6608333333333334
            )
            with pytest.raises(ValueError, match="Preview changed"):
                await hass.services.async_call(
                    "oep_test_fixture",
                    "repair_energy_balance",
                    {"apply": True, "expected_revision": "stale"},
                    blocking=True,
                    return_response=True,
                )
            safe_time = dt_util.utcnow().replace(
                minute=dt_util.utcnow().minute // 5 * 5 + 2, second=30
            )
            boundary = safe_time.replace(minute=safe_time.minute // 5 * 5, second=2)
            from types import SimpleNamespace

            from test_environment.oep_test_fixture import balance

            ha_clock = dt_util.utcnow
            with monkeypatch.context() as boundary_clock:
                boundary_clock.setattr(balance, "dt_util", SimpleNamespace(utcnow=lambda: boundary))
                assert dt_util.utcnow is ha_clock
                with pytest.raises(ValueError, match="compilation boundary"):
                    await hass.services.async_call(
                        "oep_test_fixture",
                        "repair_energy_balance",
                        {"apply": True, "expected_revision": preview["revision"]},
                        blocking=True,
                        return_response=True,
                    )
            assert (
                await Store(hass, 1, "oep_test_balance_repairs." + entry.entry_id).async_load()
                is None
            )
            with monkeypatch.context() as repair_clock:
                repair_clock.setattr(balance, "dt_util", SimpleNamespace(utcnow=lambda: safe_time))
                assert dt_util.utcnow is ha_clock
                with monkeypatch.context() as preflight:
                    preflight.setattr(balance, "get_metadata", lambda *args, **kwargs: {})
                    with pytest.raises(KeyError):
                        await hass.services.async_call(
                            "oep_test_fixture",
                            "repair_energy_balance",
                            {"apply": True, "expected_revision": preview["revision"]},
                            blocking=True,
                            return_response=True,
                        )
                assert (
                    await Store(hass, 1, "oep_test_balance_repairs." + entry.entry_id).async_load()
                    is None
                )
                applied = await hass.services.async_call(
                    "oep_test_fixture",
                    "repair_energy_balance",
                    {"apply": True, "expected_revision": preview["revision"]},
                    blocking=True,
                    return_response=True,
                )
            assert balance.dt_util is dt_util
            assert applied["applied"] is True
            assert applied["journal_status"] == "completed"
            for period in ("hour", "5minute"):
                rows = await recorder.async_add_executor_job(
                    statistics_during_period,
                    hass,
                    start,
                    None,
                    {"sensor.test_export"},
                    period,
                    {"energy": "kWh"},
                    {"sum", "state"},
                )
                values = rows["sensor.test_export"]
                assert values[-1]["state"] == pytest.approx(102)
                assert values[-1]["sum"] - values[0]["sum"] == pytest.approx(1.8308333333333333)
                assert all(
                    current["sum"] >= previous["sum"] - 1e-8
                    for previous, current in pairwise(values)
                )
            again = await hass.services.async_call(
                "oep_test_fixture",
                "repair_energy_balance",
                {"apply": True, "expected_revision": preview["revision"]},
                blocking=True,
                return_response=True,
            )
            assert again["repairs"] == [] and again["applied"] is False
            assert manager.data == before
            assert (
                await Store(hass, 1, "oep_test_balance_repairs." + entry.entry_id).async_load()
            )["status"] == "completed"
            repair_store = Store(hass, 1, "oep_test_balance_repairs." + entry.entry_id)
            completed = await repair_store.async_load()
            assert completed["before"]["hour"]["sensor.test_export"][0]["state"] == pytest.approx(
                102
            )
            await repair_store.async_save({**completed, "status": "applying"})
            with pytest.raises(ValueError, match="outcome is ambiguous"):
                await hass.services.async_call(
                    "oep_test_fixture",
                    "repair_energy_balance",
                    {"apply": True, "expected_revision": preview["revision"]},
                    blocking=True,
                    return_response=True,
                )
            with pytest.raises(ValueError, match="exact interrupted revision"):
                await hass.services.async_call(
                    "oep_test_fixture",
                    "repair_energy_balance",
                    {"resolve_ambiguous_revision": "stale"},
                    blocking=True,
                    return_response=True,
                )
            recovered = await hass.services.async_call(
                "oep_test_fixture",
                "repair_energy_balance",
                {"resolve_ambiguous_revision": preview["revision"]},
                blocking=True,
                return_response=True,
            )
            assert recovered["journal_status"] == "completed" and not recovered["applied"]
            incomplete = deepcopy(completed)
            incomplete["status"] = "applying"
            for entities in incomplete["after"].values():
                for rows in entities.values():
                    rows[0]["sum"] += 1
            await repair_store.async_save(incomplete)
            with pytest.raises(ValueError, match="mixed or unverifiable"):
                await hass.services.async_call(
                    "oep_test_fixture",
                    "repair_energy_balance",
                    {"resolve_ambiguous_revision": preview["revision"]},
                    blocking=True,
                    return_response=True,
                )
            # An interrupted operation that queued nothing can close without any write.
            incomplete["before"] = completed["after"]
            await repair_store.async_save(incomplete)
            aborted = await hass.services.async_call(
                "oep_test_fixture",
                "repair_energy_balance",
                {"resolve_ambiguous_revision": preview["revision"]},
                blocking=True,
                return_response=True,
            )
            assert aborted["journal_status"] == "aborted" and not aborted["applied"]
            await repair_store.async_save(completed)
            from test_environment.oep_test_fixture.consumer import setup_consumer

            await setup_consumer(hass, entry)
            heat_entity = registry.async_get_entity_id(
                "sensor", "oep_test_fixture", "forecast_heat_pump_energy"
            )
            # .2 kWh uncontrolled load times 20 percent, added to its 100-kWh origin.
            assert float(hass.states.get(heat_entity).state) == pytest.approx(100.04)

            from homeassistant.components.recorder.statistics import (
                _compile_hourly_statistics,
                get_metadata,
            )
            from homeassistant.components.recorder.util import session_scope

            export_metadata = (
                await recorder.async_add_executor_job(
                    lambda: get_metadata(hass, statistic_ids={"sensor.test_export"})
                )
            )["sensor.test_export"][1]
            # Recorder compiles every series. Use an unseeded hour so retained
            # fixture history, including the boiler, is never compiled twice.
            future = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(
                hours=1
            )
            # MWh source. The next hour adds exactly .2 kWh after the repaired total.
            recorder.async_import_statistics(
                export_metadata,
                [
                    {
                        "start": future + timedelta(minutes=55),
                        "sum": (values[-1]["sum"] + 0.2) / 1000,
                        "state": 102.2 / 1000,
                    }
                ],
                StatisticsShortTerm,
            )
            await recorder.async_block_till_done()

            def compile_future():
                """Exercise HA's actual last-fine-sum hourly compilation after repair."""
                with session_scope(session=recorder.get_session()) as session:
                    _compile_hourly_statistics(session, future)

            await recorder.async_add_executor_job(compile_future)
            future_rows = await recorder.async_add_executor_job(
                statistics_during_period,
                hass,
                start + timedelta(hours=2),
                None,
                {"sensor.test_export"},
                "hour",
                {"energy": "kWh"},
                {"sum"},
            )
            assert future_rows["sensor.test_export"][-1]["sum"] - future_rows["sensor.test_export"][
                0
            ]["sum"] == pytest.approx(0.2)

    asyncio.run(scenario())
