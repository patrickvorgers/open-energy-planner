"""Verify real HA Energy metadata, source membership and setup catalogue exchange."""

import asyncio
from datetime import timedelta

import pytest

pytest.importorskip(
    "homeassistant", reason="Real HA integration suite runs in the pinned container"
)

from aiohttp import ClientSession
from homeassistant.util import dt as dt_util

from custom_components.open_energy_planner.const import DOMAIN
from tests.custom_component.support.chain import TOKEN, app_write, find, running_chain, setup_energy


def test_large_discovery_transfers_tail_vehicle_in_bounded_complete_snapshot(monkeypatch):
    """A car after 2,000 unrelated sensors crosses real HA HTTP discovery without truncation."""
    import json

    async def scenario():
        """Capture real bounded requests while the app assembles and validates the full snapshot."""
        async with running_chain(live=True) as (hass, entry, url, _root):
            for index in range(2000):
                hass.states.async_set(
                    f"sensor.synthetic_meter_{index:04}",
                    "1",
                    {
                        "friendly_name": f"Independent synthetic meter {index}",
                        "unit_of_measurement": "W",
                    },
                )
            hass.states.async_set(
                "sensor.zzz_family_vehicle_soc",
                "65",
                {
                    "friendly_name": "Family vehicle battery",
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                },
            )
            coordinator = entry.runtime_data
            request = coordinator.client.request
            exchanges = []

            async def record_request(method, path, body=None):
                """Record only independently generated discovery messages and actual acknowledgements."""
                result = await request(method, path, body)
                if path == "/v1/ha/catalogue":
                    exchanges.append((len(json.dumps(body).encode()), result["accepted"]))
                return result

            monkeypatch.setattr(coordinator.client, "request", record_request)
            coordinator.catalogue_sent = None
            await coordinator.sync()
            async with (
                ClientSession() as session,
                session.get(
                    url + "/v1/ev", headers={"Authorization": f"Bearer {TOKEN}"}
                ) as response,
            ):
                assert response.status == 200
                state = await response.json()
            entities = state["catalogue"]["entities"]
            assert (
                sum(
                    entity["entity_id"].startswith("sensor.synthetic_meter_") for entity in entities
                )
                == 2000
            )
            assert (
                next(
                    entity
                    for entity in entities
                    if entity["entity_id"] == "sensor.zzz_family_vehicle_soc"
                )["unit"]
                == "%"
            )
            assert len(exchanges) > 2
            assert all(size <= 196608 for size, _accepted in exchanges)
            assert all(not accepted for _size, accepted in exchanges[:-1])
            assert exchanges[-1][1] is True

    asyncio.run(scenario())


def test_build_change_during_optional_catalogue_exchange_is_not_suppressed(monkeypatch):
    """A build mismatch after a matching snapshot still reaches the HA action-required policy."""
    from open_energy_planner import build_info

    async def scenario():
        """Swap app identity between real HTTP requests rather than injecting a validation exception."""
        async with running_chain(live=True) as (_hass, entry, _url, _root):
            coordinator = entry.runtime_data
            snapshot = coordinator.client.snapshot

            async def snapshot_then_change_build():
                """Return a matching snapshot, then emulate a different app build before catalogue refresh."""
                data = await snapshot()
                monkeypatch.setattr(
                    build_info, "BUILD_INFO", {**build_info.BUILD_INFO, "build": "999.1"}
                )
                return data

            monkeypatch.setattr(coordinator.client, "snapshot", snapshot_then_change_build)
            coordinator.catalogue_sent = None
            await coordinator.sync()
            assert coordinator.issue == "release_mismatch"

    asyncio.run(scenario())


@pytest.mark.parametrize("capacity", [float("nan"), float("inf"), -1, True, "10"])
def test_invalid_energy_battery_fields_are_visible_missing_settings(capacity):
    """Oversized links and nonfinite capacity become explicit missing fields instead of blocking the entire catalogue upload."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from custom_components.open_energy_planner.catalogue import setup_catalogue
    from open_energy_planner.home_assistant.catalogue import HACatalogue

    async def scenario():
        """Supply malformed metadata through a read-only manager stub without corrupting HA's live manager/Recorder."""
        async with running_chain(live=True) as (hass, _entry, _url, _root):
            metadata = {
                "energy_sources": [
                    {
                        "type": "grid",
                        "stat_energy_from": "sensor.import",
                        "stat_rate": "sensor.grid",
                    },
                    {
                        "type": "battery",
                        "stat_energy_from": "sensor.discharge",
                        "stat_energy_to": "sensor." + "x" * 200,
                        "stat_soc": "sensor." + "x" * 200,
                        "capacity": capacity,
                    },
                ]
            }
            with patch(
                "homeassistant.components.energy.data.async_get_manager",
                AsyncMock(return_value=SimpleNamespace(data=metadata)),
            ):
                body = await setup_catalogue(hass)
            battery = body["suggestions"]["batteries"][0]
            assert battery["capacity_kwh"] is None
            assert battery["soc_entity"] is None
            assert battery["charge_energy_entity"] is None
            HACatalogue().receive(body, dt_util.utcnow())

    asyncio.run(scenario())


def test_startup_refresh_replaces_early_missing_energy_catalogue():
    """Energy/entities loaded after the first integration sync appear at HA startup, without a manual refresh or five-minute delay."""
    from homeassistant.components.energy.data import async_get_manager

    async def loaded(hass, _entry, _url):
        """Load physical metadata after OEP's first sync, matching independent integration startup order."""
        manager = await async_get_manager(hass)
        await manager.async_update(
            {
                "energy_sources": [
                    {
                        "type": "grid",
                        "stat_energy_from": "sensor.import",
                        "stat_rate": "sensor.grid",
                    }
                ],
                "device_consumption": [],
            }
        )
        hass.states.async_set(
            "sensor.grid",
            0,
            {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
        )
        hass.states.async_set(
            "sensor.import",
            100,
            {
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
            },
        )
        from unittest.mock import patch

        from custom_components.open_energy_planner.catalogue import setup_catalogue

        assert not hass.is_running
        with patch(
            "custom_components.open_energy_planner.setup_history.setup_history",
            side_effect=AssertionError("Recorder must wait for Core startup"),
        ):
            early = await setup_catalogue(hass)
        assert early["suggestions"]["energy_status"] == "ready"
        assert early["history"] == []

    async def scenario():
        """Use real Core startup event dispatch and the authenticated integration/app transport."""
        async with running_chain(live=True, recorder=True, before_start=loaded) as (
            hass,
            entry,
            url,
            _root,
        ):
            await hass.async_block_till_done()
            assert entry.runtime_data.startup_cancel is None
            async with (
                ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
                session.get(url + "/v1/setup") as response,
            ):
                data = await response.json()
            assert data["catalogue"]["data"]["suggestions"]["energy_status"] == "ready"
            assert (
                next(
                    entity
                    for entity in data["catalogue"]["data"]["entities"]
                    if entity["entity_id"] == "sensor.grid"
                )["unit"]
                == "W"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("source_kind", ["grid", "battery"])
@pytest.mark.parametrize("power_mode", ["none", "standard", "inverted", "two"])
def test_energy_power_modes_use_ha_normalization(source_kind, power_mode):
    """Both device types reuse HA normalization: 1.5 kW or 2000-500 W is +1500 W, with reversed flow -1500 W."""
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager

    from custom_components.open_energy_planner.catalogue import energy_suggestions
    from custom_components.open_energy_planner.measurement_sources import (
        HomeAssistantMeasurementSource,
    )
    from custom_components.open_energy_planner.measurements import Quantity, SourceBinding

    async def scenario():
        """Create genuine Energy entities and read their published states through OEP, without mocking arithmetic."""
        async with running_chain(recorder=True) as (hass, _entry, _url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            attributes = {
                "unit_of_measurement": "kW",
                "device_class": "power",
                "state_class": "measurement",
            }
            hass.states.async_set("sensor.raw_power", "1.5", attributes)
            hass.states.async_set("sensor.from_power", "2", attributes)
            hass.states.async_set(
                "sensor.to_power", "500", {**attributes, "unit_of_measurement": "W"}
            )
            source = {
                "type": source_kind,
                "stat_energy_from": "sensor.import",
                "stat_energy_to": "sensor.export",
            }
            configurations = {
                "standard": {"stat_rate": "sensor.raw_power"},
                "inverted": {"stat_rate_inverted": "sensor.raw_power"},
                "two": {"stat_rate_from": "sensor.from_power", "stat_rate_to": "sensor.to_power"},
            }
            if power_mode != "none":
                source["power_config"] = configurations[power_mode]
            manager = await async_get_manager(hass)
            await manager.async_update({"energy_sources": [source], "device_consumption": []})
            assert await setup_energy(hass)
            await hass.async_block_till_done()
            hints = await energy_suggestions(hass)
            entity_id = (
                hints["grid_power"]
                if source_kind == "grid"
                else hints["batteries"][0]["power_entity"]
            )
            if power_mode == "none":
                assert entity_id is None
                return
            assert entity_id == manager.data["energy_sources"][0]["stat_rate"]
            reader = HomeAssistantMeasurementSource(hass.states)
            binding = SourceBinding(source_kind + ".power", entity_id, Quantity.POWER)
            # HA keeps state report times monotonic even when the test clock catches up.
            captured = max(dt_util.utcnow(), hass.states.get(entity_id).last_reported)
            batch = reader.read([binding], captured)
            assert batch.ready, batch.issues
            assert batch.readings[0].value == (-1500 if power_mode == "inverted" else 1500)
            assert batch.readings[0].unit == "W"
            if power_mode == "two":
                hass.states.async_set("sensor.from_power", "0.5", attributes)
                hass.states.async_set(
                    "sensor.to_power", "2000", {**attributes, "unit_of_measurement": "W"}
                )
            else:
                hass.states.async_set("sensor.raw_power", "-1.5", attributes)
            await hass.async_block_till_done()
            # HA keeps state report times monotonic even when the test clock catches up.
            captured = max(dt_util.utcnow(), hass.states.get(entity_id).last_reported)
            batch = reader.read([binding], captured)
            assert batch.ready, batch.issues
            assert batch.readings[0].value == (1500 if power_mode == "inverted" else -1500)

    asyncio.run(scenario())


@pytest.mark.parametrize("source_kind", ["grid", "battery"])
@pytest.mark.parametrize("missing_side", ["from", "to"])
@pytest.mark.parametrize("failure", ["absent", "unknown", "unavailable", "removed"])
def test_energy_two_power_sensors_require_both_and_recover(source_kind, missing_side, failure):
    """A missing/failed half never becomes zero or a stale reading. Restoring both halves yields 2000-500=1500 W."""
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager

    from custom_components.open_energy_planner.catalogue import energy_suggestions
    from custom_components.open_energy_planner.measurement_sources import (
        HomeAssistantMeasurementSource,
    )
    from custom_components.open_energy_planner.measurements import Quantity, SourceBinding

    async def scenario():
        """Exercise actual HA sensor creation, state-change listeners, disappearance and recovery through OEP's reader."""
        async with running_chain(recorder=True) as (hass, _entry, _url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            attributes = {
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
            }
            for side, power_w in (("from", 2000), ("to", 500)):
                if failure != "absent" or side != missing_side:
                    hass.states.async_set("sensor." + side + "_power", power_w, attributes)
            manager = await async_get_manager(hass)
            await manager.async_update(
                {
                    "energy_sources": [
                        {
                            "type": source_kind,
                            "stat_energy_from": "sensor.import",
                            "stat_energy_to": "sensor.export",
                            "power_config": {
                                "stat_rate_from": "sensor.from_power",
                                "stat_rate_to": "sensor.to_power",
                            },
                        }
                    ],
                    "device_consumption": [],
                }
            )
            assert await setup_energy(hass)
            await hass.async_block_till_done()
            hints = await energy_suggestions(hass)
            entity_id = (
                hints["grid_power"]
                if source_kind == "grid"
                else hints["batteries"][0]["power_entity"]
            )
            assert entity_id
            binding = SourceBinding(source_kind + ".power", entity_id, Quantity.POWER)
            reader = HomeAssistantMeasurementSource(hass.states)
            failed_id = "sensor." + missing_side + "_power"
            if failure != "absent":
                assert reader.read([binding], dt_util.utcnow()).readings[0].value == 1500
                if failure == "removed":
                    hass.states.async_remove(failed_id)
                else:
                    hass.states.async_set(failed_id, failure, attributes)
                await hass.async_block_till_done()
            batch = reader.read([binding], dt_util.utcnow())
            assert not batch.ready
            assert not batch.readings
            assert [(issue.key, issue.reason, issue.required) for issue in batch.issues] == [
                (source_kind + ".power", "unavailable", True)
            ]
            assert (await energy_suggestions(hass))[
                "batteries" if source_kind == "battery" else "grid_power_entities"
            ]
            hass.states.async_set(failed_id, 2000 if missing_side == "from" else 500, attributes)
            await hass.async_block_till_done()
            batch = reader.read([binding], dt_util.utcnow())
            assert batch.ready, batch.issues
            assert batch.readings[0].value == 1500
            assert batch.readings[0].unit == "W"

    asyncio.run(scenario())


def test_app_source_selection_saves_only_in_app_and_forwards_real_reports():
    """Configure app-owned sources. Persist configuration only in the app and keep unavailable optional sensors nonblocking."""

    async def scenario():
        """Configure sources through authenticated HTTP and read actual HA state without integration-owned persistence."""
        async with running_chain(live=True) as (hass, entry, url, root):
            for entity, value, unit in (
                ("sensor.input_grid", "0", "W"),
                ("sensor.input_soc", "65", "%"),
                ("sensor.input_battery", "-0.25", "kW"),
                ("sensor.input_export", "123.5", "kWh"),
            ):
                hass.states.async_set(entity, value, {"unit_of_measurement": unit})
            await app_write(
                url,
                "sources",
                {
                    "bindings": [
                        {
                            "key": "grid.power",
                            "entity_id": "sensor.input_grid",
                            "quantity": "power",
                        },
                        {
                            "key": "grid.export_energy",
                            "entity_id": "sensor.input_export",
                            "quantity": "energy",
                        },
                        {"key": "battery.soc", "entity_id": "sensor.input_soc", "quantity": "soc"},
                        {
                            "key": "battery.power",
                            "entity_id": "sensor.input_battery",
                            "quantity": "power",
                            "multiplier": -1,
                        },
                        {
                            "key": "household.power",
                            "entity_id": "sensor.unavailable_household",
                            "quantity": "power",
                            "required": False,
                        },
                        {
                            "key": "pv.power",
                            "entity_id": "sensor.unavailable_pv",
                            "quantity": "power",
                            "required": False,
                        },
                    ]
                },
            )
            await entry.runtime_data.sync()
            assert not entry.options
            assert set(entry.data) == {"url", "token"}
            async with (
                ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
                session.get(url + "/v1/snapshot") as response,
            ):
                state = await response.json()
            assert state["plan"] is None
            assert state["planning"]["reason"] == "forecast_or_prices_unavailable"
            assert state["observations"]["measurement_version"]
            bindings = {raw["key"]: raw for raw in state["measurement_config"]["bindings"]}
            assert len(bindings) == 6
            assert bindings["grid.export_energy"]["required"] is True
            assert bindings["grid.export_energy"]["quantity"] == "energy"
            from custom_components.open_energy_planner.observation_bridge import ObservationBridge

            forwarded = []
            original_capture = ObservationBridge.capture

            def observe_capture(bridge, *args, **kwargs):
                """Observe the actual coordinator's forwarded HA location without changing its report."""
                ready, report = original_capture(bridge, *args, **kwargs)
                forwarded.append(report)
                return ready, report

            from unittest.mock import patch

            with patch.object(ObservationBridge, "capture", observe_capture):
                await entry.runtime_data.sync()
            assert forwarded[-1]["location"] == {
                "latitude": hass.config.latitude,
                "longitude": hass.config.longitude,
                "timezone": hass.config.time_zone,
            }
            export = next(
                item for item in forwarded[-1]["readings"] if item["key"] == "grid.export_energy"
            )
            assert export["value"] == 123.5
            assert export["unit"] == "kWh"
            assert bindings["household.power"]["required"] is False
            assert bindings["pv.power"]["required"] is False
            assert bindings["battery.soc"]["required"] is True
            assert {issue["key"] for issue in state["observations"]["issues"]} == {
                "household.power",
                "pv.power",
            }
            assert find(hass, "battery_power_target").state == "unavailable"

            from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

            saved = SqlPlanRepository(root / "app").state
            assert saved["measurement_config"]["revision"] == state["config_revision"]
            # Reopening edits the app-owned configuration rather than creating HA helpers.
            result = await hass.config_entries.options.async_init(entry.entry_id)
            assert result["type"] == "form", result
            assert not any(
                state_item.entity_id.startswith("input_number.")
                for state_item in hass.states.async_all()
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("required", [False, True])
def test_missing_source_alert_depends_on_its_required_flag(required):
    """Remove a configured PV source. Show an action-required notification only when that source is required."""
    from unittest.mock import patch

    from homeassistant.components import persistent_notification

    async def scenario():
        """Remove the selected source and compare required versus optional notification behavior."""
        async with (
            running_chain(live=True) as (hass, entry, url, _root),
            ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
        ):
            hass.states.async_set("sensor.grid", "0", {"unit_of_measurement": "W"})
            async with session.put(
                url + "/v1/sources",
                json={
                    "expected_revision": None,
                    "bindings": [
                        {"key": "grid.power", "entity_id": "sensor.grid", "quantity": "power"},
                        {
                            "key": "pv.power",
                            "entity_id": "sensor.absent_pv",
                            "quantity": "power",
                            "required": required,
                        },
                    ],
                },
            ) as response:
                assert response.status == 200
            with patch.object(
                persistent_notification, "async_create", wraps=persistent_notification.async_create
            ) as notify:
                await entry.runtime_data.sync()
            assert notify.called is required
            assert entry.runtime_data.issue == (
                "measurement_sources_unusable" if required else None
            )
            assert entry.runtime_data.observations["issues"][0]["reason"] == "missing_entity"

    asyncio.run(scenario())


@pytest.mark.parametrize("required", [False, True])
def test_app_source_api_preserves_app_owned_flags_and_unexposed_settings(required):
    """Edit one source through the app API. Retain another binding's required flag, sign, age and timestamp configuration."""

    async def scenario():
        """Edit one binding while reading back independent flags and settings on the untouched binding."""
        async with (
            running_chain(live=True) as (_hass, _entry, url, _root),
            ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
        ):
            async with session.put(
                url + "/v1/sources",
                json={
                    "expected_revision": None,
                    "bindings": [
                        {"key": "grid.power", "entity_id": "sensor.grid", "quantity": "power"},
                        {
                            "key": "pv.power",
                            "entity_id": "sensor.pv",
                            "quantity": "power",
                            "required": required,
                            "multiplier": -1,
                            "maximum_age_s": 120,
                            "timestamp_attribute": "sample_time",
                        },
                    ],
                },
            ) as response:
                assert response.status == 200
                before = await response.json()
            bindings = before["measurement_config"]["bindings"]
            bindings[0]["entity_id"] = "sensor.changed_grid"
            await app_write(
                url,
                "sources",
                {"bindings": bindings, "expected_revision": before["config_revision"]},
            )
            async with session.get(url + "/v1/snapshot") as response:
                after = await response.json()
            old = {binding["key"]: binding for binding in before["measurement_config"]["bindings"]}
            new = {binding["key"]: binding for binding in after["measurement_config"]["bindings"]}
            assert new["pv.power"] == old["pv.power"]
            assert new["grid.power"]["entity_id"] == "sensor.changed_grid"

    asyncio.run(scenario())


@pytest.mark.parametrize("live", [False, True])
def test_disappearing_measurement_configuration_clears_old_bridge_and_revision(live):
    """Switch the app out of configured live input. Clear the old bridge and reconcile its replacement revision."""
    from custom_components.open_energy_planner.observation_bridge import ObservationBridge

    async def scenario():
        """Replace configured live input and verify the old bridge and revision are discarded."""
        async with running_chain(live=live) as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            hass.states.async_set("sensor.old_grid", "0", {"unit_of_measurement": "W"})
            coordinator.observation_bridge = ObservationBridge(
                hass.states,
                {
                    "revision": "old-live",
                    "bindings": [
                        {
                            "key": "grid.power",
                            "entity_id": "sensor.old_grid",
                            "quantity": "power",
                            "required": True,
                            "multiplier": 1,
                            "maximum_age_s": 300,
                            "timestamp_attribute": None,
                        }
                    ],
                },
            )
            coordinator.schedule.config_revision = "old-live"
            coordinator.schedule.plan = coordinator.schedule.document = None
            await coordinator.sync()
            assert coordinator.observation_bridge is None
            assert coordinator.schedule.config_revision == "demo-1"
            assert (coordinator.schedule.plan is not None) is (not live)
            if not live:
                assert find(hass, "battery_power_target").state == "-1000"

    asyncio.run(scenario())


def test_app_source_api_does_not_overwrite_configuration_changed_elsewhere():
    """Save an editor after an external change. Reject the stale revision and preserve the external configuration."""

    async def scenario():
        """Submit a stale editor revision after an external save and retain the newer configuration."""
        async with (
            running_chain(live=True) as (_hass, _entry, url, _root),
            ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
        ):
            async with session.put(
                url + "/v1/sources",
                json={
                    "expected_revision": None,
                    "bindings": [
                        {
                            "key": "grid.power",
                            "entity_id": "sensor.other_grid",
                            "quantity": "power",
                        }
                    ],
                },
            ) as response:
                assert response.status == 200
                changed = await response.json()
            async with session.put(
                url + "/v1/sources",
                json={
                    "expected_revision": None,
                    "bindings": [
                        {
                            "key": "grid.power",
                            "entity_id": "sensor.old_grid",
                            "quantity": "power",
                        }
                    ],
                },
            ) as response:
                assert response.status == 409
            async with session.get(url + "/v1/snapshot") as response:
                current = await response.json()
            assert current["measurement_config"] == changed["measurement_config"]

    asyncio.run(scenario())


@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("grid_shape", ["routes", "flat"])
def test_installation_energy_balance_prefill_requires_all_declared_channels(complete, grid_shape):
    """Both HA grid schemas retain configured channels even with incompatible live metadata. Preview validates units separately."""
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager

    from custom_components.open_energy_planner.catalogue import energy_suggestions

    async def scenario():
        """Read actual Energy preferences and HA states. Never silently drop a required balance term."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await setup_energy(hass)
            manager = await async_get_manager(hass)
            sources = [
                {
                    "type": "grid",
                    "flow_from": [{"stat_energy_from": "sensor.incoming"}],
                    "flow_to": [{"stat_energy_to": "sensor.outgoing"}],
                },
                {
                    "type": "solar",
                    "stat_energy_from": "sensor.roof",
                    "config_entry_solar_forecast": None,
                },
                {
                    "type": "battery",
                    "stat_energy_from": "sensor.discharge",
                    "stat_energy_to": "sensor.charge",
                },
            ]
            if grid_shape == "flat":
                sources[0] = {
                    "type": "grid",
                    "stat_energy_from": "sensor.incoming",
                    "stat_energy_to": "sensor.outgoing",
                }
            await manager.async_update({"energy_sources": sources, "device_consumption": []})
            for name in ("incoming", "outgoing", "roof", "discharge", "charge"):
                hass.states.async_set(
                    "sensor." + name,
                    "100",
                    {
                        "unit_of_measurement": "W" if name == "charge" and not complete else "kWh",
                        "device_class": "energy",
                        "state_class": "total_increasing",
                    },
                )
            result = await energy_suggestions(hass)
            expected = {
                "sensor.incoming": 1,
                "sensor.outgoing": -1,
                "sensor.roof": 1,
                "sensor.discharge": 1,
                "sensor.charge": -1,
            }
            assert {
                energy_term["entity_id"]: energy_term["sign"] for energy_term in result["terms"]
            } == expected
            assert result["energy_status"] == "ready"
            assert manager.data["energy_sources"] == sources

    asyncio.run(scenario())


def test_ha_options_only_link_to_app_and_push_fresh_setup_metadata():
    """HA exposes no sensor/device wizard. Opening app setup requests fresh read-only sensor choices without storing configuration in HA."""

    async def scenario():
        """Verify actual HA options, local app discovery and bounded sensor metadata."""
        async with running_chain(live=True) as (hass, entry, url, _root):
            hass.states.async_set(
                "sensor.later_grid",
                "42",
                {
                    "unit_of_measurement": "W",
                    "friendly_name": "Later grid",
                    "secret_attribute": "never-export",
                },
            )
            form = await hass.config_entries.options.async_init(entry.entry_id)
            assert form["step_id"] == "init"
            assert not form["data_schema"].schema
            assert form["description_placeholders"]["url"] == url + "/setup"
            async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
                async with session.get(url + "/v1/setup?refresh=1") as response:
                    assert response.status == 200
                await entry.runtime_data.sync()
                async with session.get(url + "/v1/setup") as response:
                    data = await response.json()
            grid = next(
                entity_item
                for entity_item in data["catalogue"]["data"]["entities"]
                if entity_item["entity_id"] == "sensor.later_grid"
            )
            assert grid["unit"] == "W"
            assert "state" not in grid and "secret_attribute" not in grid
            assert not entry.options
            assert data["installation"] is None

    asyncio.run(scenario())


def test_optional_setup_catalogue_failure_does_not_block_current_plan(monkeypatch):
    """A failed optional metadata upload cannot prevent accepting/maintaining a valid plan or advancing its normal HA projection."""
    from aiohttp import ClientError

    async def scenario():
        """Fail only discovery transport while real snapshot, acknowledgement and schedule validation continue."""
        async with running_chain() as (_hass, entry, _url, _root):
            coordinator = entry.runtime_data
            request = coordinator.client.request
            attempts = 0
            refresh = "setup-one"

            async def fail_discovery(method, path, body=None):
                """Retain the genuine authenticated app and exercise only the optional catalogue failure path."""
                nonlocal attempts
                if path == "/v1/ha/catalogue":
                    if "refresh_failed" not in (body or {}):
                        attempts += 1
                    raise ClientError("Discovery temporarily unavailable")
                data = await request(method, path, body)
                if path == "/v1/snapshot":
                    data["source"] = "home_assistant"
                    data["installation"]["setup_discovery_requested"] = refresh
                return data

            monkeypatch.setattr(coordinator.client, "request", fail_discovery)
            await coordinator.sync()
            assert coordinator.projection.status == "active"
            assert coordinator.projection.command is not None
            await coordinator.sync()
            assert attempts == 1  # A still-pending request is not a new user refresh.
            refresh = "setup-two"
            await coordinator.sync()
            assert attempts == 2
            coordinator.catalogue_failed_at -= timedelta(minutes=6)
            await coordinator.sync()
            assert attempts == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("large_balance", [False, True])
@pytest.mark.parametrize("long_battery", [False, True])
def test_ha_setup_catalogue_skips_long_ids_and_preserves_large_valid_balance(
    large_balance, long_battery
):
    """Invalid identities cannot poison discovery, while every valid Energy balance channel is retained."""
    import json

    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager

    from custom_components.open_energy_planner.catalogue import setup_catalogue
    from custom_components.open_energy_planner.catalogue_transfer import catalogue_pages
    from open_energy_planner.home_assistant.catalogue import HACatalogue

    async def scenario():
        """Validate the real HA adapter's output against the app consumer rather than duplicating its wire constraints."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await setup_energy(hass)
            manager = await async_get_manager(hass)
            count = 17 if large_balance else 2
            for index in range(count):
                hass.states.async_set(
                    f"sensor.energy{index}",
                    "100",
                    {
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "total_increasing",
                    },
                )
            long_id = "sensor." + "x" * 180
            hass.states.async_set(long_id, "100", {"unit_of_measurement": "kWh"})
            for index in range(300):
                hass.states.async_set(
                    f"sensor.unicode{index}",
                    "0",
                    {"unit_of_measurement": "W", "friendly_name": "Ω" * 160},
                )
            await manager.async_update(
                {
                    "energy_sources": [
                        {
                            "type": "grid",
                            "flow_from": [
                                {"stat_energy_from": f"sensor.energy{index}"}
                                for index in range(count)
                            ],
                            "flow_to": [],
                        }
                    ],
                    "device_consumption": [],
                }
            )
            if long_battery:
                sources = manager.data["energy_sources"] + [
                    {
                        "type": "battery",
                        "stat_energy_from": long_id,
                        "stat_energy_to": "sensor.energy0",
                    }
                ]
                await manager.async_update({"energy_sources": sources, "device_consumption": []})
            body = await setup_catalogue(hass)
            if long_battery:
                assert body["suggestions"]["batteries"] == []
                assert body["suggestions"]["energy_status"] == "incomplete"
            assert (
                sum(entity["entity_id"].startswith("sensor.unicode") for entity in body["entities"])
                == 300
            )
            assert all(
                len(json.dumps(page).encode()) <= 192 * 1024 for page in catalogue_pages(body)
            )
            assert all(entity_item["entity_id"] != long_id for entity_item in body["entities"])
            assert len(body["suggestions"]["terms"]) == (0 if long_battery else count)
            catalogue = HACatalogue()
            catalogue.receive(body, dt_util.utcnow())
            assert catalogue.current(dt_util.utcnow())["stale"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize("consumer_count", [0, 256])
def test_setup_catalogue_budgets_year_history_and_preserves_preferred_entities(
    monkeypatch, consumer_count
):
    """Large Unicode metadata and all year endpoints reach the real app within its dedicated 1 MiB budget."""
    import json

    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager

    from custom_components.open_energy_planner.catalogue import setup_catalogue
    from custom_components.open_energy_planner.catalogue_transfer import catalogue_pages

    async def scenario():
        """Exercise real HA discovery and HTTP admission with independently supplied year endpoints."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await setup_energy(hass)
            end_day = int(dt_util.utcnow().timestamp()) // 86400
            meters = ["sensor.zz_energy_" + "x" * 90 + str(index) for index in range(16)]
            consumers = [
                "sensor.zz_consumer_" + "x" * 90 + f"_{index:03}" for index in range(consumer_count)
            ]
            powers = [
                "sensor.zz_power_" + "x" * 90 + f"_{index:03}" for index in range(consumer_count)
            ]
            preferred = {*meters, *consumers, *powers, "sensor.zz_grid_power"}
            for entity in [
                *sorted(preferred),
                *(f"sensor.extra_{index:04}" for index in range(400)),
            ]:
                is_power = entity == "sensor.zz_grid_power" or entity in powers
                hass.states.async_set(
                    entity,
                    "100",
                    {
                        "unit_of_measurement": "W" if is_power else "kWh",
                        "device_class": "power" if is_power else "energy",
                        "state_class": "measurement" if is_power else "total_increasing",
                        "friendly_name": "Energy meter" if entity in preferred else "Ω" * 160,
                    },
                )
            manager = await async_get_manager(hass)
            await manager.async_update(
                {
                    "energy_sources": [
                        {
                            "type": "grid",
                            "stat_energy_from": entity,
                            "stat_rate": "sensor.zz_grid_power",
                        }
                        for entity in meters
                    ],
                    "device_consumption": [
                        {
                            "stat_consumption": entity,
                            "included_in_stat": consumers[0] if index else None,
                            "power_config": {"stat_rate": powers[index]},
                        }
                        for index, entity in enumerate(consumers)
                    ],
                }
            )
            history = [
                {
                    "entity_id": entity,
                    "points": [
                        [
                            float((end_day - 365 + day_offset) * 86400),
                            1234567890123.125 + day_offset * 10,
                        ]
                        for day_offset in (0, 365)
                    ],
                }
                for entity in meters
            ]

            async def year_history(_hass, terms):
                """Supply compact independent Recorder outputs without mocking catalogue serialization or HTTP."""
                assert {term["entity_id"] for term in terms} == set(meters)
                return history

            monkeypatch.setattr(
                "custom_components.open_energy_planner.setup_history.setup_history", year_history
            )
            body = await setup_catalogue(hass)
            encoded = json.dumps(body).encode("utf-8")
            if consumer_count:
                assert len(encoded) > 1024 * 1024
            async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
                if not consumer_count:
                    async with session.post(
                        url + "/v1/ha/catalogue",
                        data=encoded + b" " * (1024 * 1024 + 1 - len(encoded)),
                        headers={"Content-Type": "application/json"},
                    ) as response:
                        assert response.status == 413
                for page in catalogue_pages(body):
                    assert len(json.dumps(page).encode()) <= 192 * 1024
                    async with session.post(url + "/v1/ha/catalogue", json=page) as response:
                        assert response.status == 200
                async with session.get(url + "/v1/setup") as response:
                    setup = await response.json()
            entity_ids = [entity["entity_id"] for entity in body["entities"]]
            assert preferred <= set(entity_ids)
            assert set(entity_ids[: len(preferred)]) == preferred
            assert sum(entity.startswith("sensor.extra_") for entity in entity_ids) == 400
            assert body["history"] == history
            assert setup["annual_estimate"]["annual_kwh"] == 58400
            assert setup["annual_estimate"]["days"] == 365
            assert setup["annual_estimate"]["extrapolated"] is False

    asyncio.run(scenario())


def test_setup_catalogue_keeps_ordinary_sensors_before_large_binary_population():
    """More than 1024 binary states cannot evict ordinary EV SoC/power metadata or preferred Energy sources."""

    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager

    from custom_components.open_energy_planner.catalogue import setup_catalogue
    from open_energy_planner.home_assistant.catalogue import HACatalogue

    async def scenario():
        """Use real HA states and Energy settings, retaining binary classes and deterministic group ordering."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await setup_energy(hass)
            hass.states.async_set("sensor.zzzz_energy", "100", {"unit_of_measurement": "kWh"})
            hass.states.async_set("sensor.zz_ev_soc", "60", {"unit_of_measurement": "%"})
            hass.states.async_set("sensor.zz_ev_power", "1500", {"unit_of_measurement": "W"})
            for index in reversed(range(1025)):
                hass.states.async_set(
                    f"binary_sensor.flood{index:04}",
                    "on",
                    {"device_class": "motion"} if index % 2 == 0 else {},
                )
            manager = await async_get_manager(hass)
            await manager.async_update(
                {
                    "energy_sources": [
                        {"type": "grid", "stat_energy_from": "sensor.zzzz_energy"},
                        {"type": "grid", "stat_energy_from": "sensor.zzzzz_missing_energy"},
                    ],
                    "device_consumption": [],
                }
            )
            body = await setup_catalogue(hass)
            entity_ids = [entity["entity_id"] for entity in body["entities"]]
            assert len(entity_ids) > 1024
            assert entity_ids.index("sensor.zzzz_energy") < entity_ids.index("sensor.zz_ev_power")
            assert entity_ids.index("sensor.zzzzz_missing_energy") < entity_ids.index(
                "sensor.zz_ev_power"
            )
            missing = next(
                entity
                for entity in body["entities"]
                if entity["entity_id"] == "sensor.zzzzz_missing_energy"
            )
            assert missing["unit"] == missing["device_class"] == missing["state_class"] == ""
            assert "state" not in missing
            assert hass.states.get("sensor.zzzzz_missing_energy") is None
            assert entity_ids.index("sensor.zz_ev_power") < entity_ids.index("sensor.zz_ev_soc")
            binary_ids = [
                identity for identity in entity_ids if identity.startswith("binary_sensor.")
            ]
            assert binary_ids == sorted(binary_ids)
            assert {"binary_sensor.flood0000", "binary_sensor.flood0001"} <= set(binary_ids)
            assert entity_ids.index("sensor.zz_ev_soc") < entity_ids.index(binary_ids[0])
            HACatalogue().receive(body, dt_util.utcnow())

    asyncio.run(scenario())


def test_energy_device_connection_survives_a_large_unrelated_sensor_catalogue():
    """A charger's registered connection and integration title survive more than 1024 unrelated sensors."""

    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from custom_components.open_energy_planner.catalogue import setup_catalogue
    from open_energy_planner.home_assistant.catalogue import HACatalogue

    async def scenario():
        """Exercise genuine HA registry links, active states, Energy ownership and bounded serialization."""
        async with running_chain(live=True, recorder=True) as (hass, entry, _url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await setup_energy(hass)
            hass.config_entries.async_update_entry(entry, title="Garage charger")
            device = dr.async_get(hass).async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={("fixture", "garage-charger")},
                name="Garage charger",
            )
            registry = er.async_get(hass)
            meter = registry.async_get_or_create(
                "sensor",
                "fixture",
                "charging-meter",
                config_entry=entry,
                device_id=device.id,
                suggested_object_id="charging_meter",
            )
            connected = registry.async_get_or_create(
                "binary_sensor",
                "fixture",
                "charging-plug",
                config_entry=entry,
                device_id=device.id,
                suggested_object_id="zzzz_charging_plug",
            )
            hass.states.async_set(
                meter.entity_id,
                "1",
                {
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                },
            )
            hass.states.async_set(connected.entity_id, "on", {"device_class": "plug"})
            hass.states.async_set(
                "sensor.grid_import",
                "1",
                {
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                },
            )
            for index in range(1100):
                hass.states.async_set(f"sensor.unrelated_{index:04}", "0")
            manager = await async_get_manager(hass)
            await manager.async_update(
                {
                    "energy_sources": [{"type": "grid", "stat_energy_from": "sensor.grid_import"}],
                    "device_consumption": [{"stat_consumption": meter.entity_id}],
                }
            )
            body = await setup_catalogue(hass)
            rows = {row["entity_id"]: row for row in body["entities"]}
            assert not ({meter.entity_id, connected.entity_id} - rows.keys())
            order = list(rows)
            assert order.index(meter.entity_id) < order.index(connected.entity_id)
            assert order.index(connected.entity_id) < order.index("sensor.unrelated_0000")
            assert rows[connected.entity_id]["integration_id"] == entry.entry_id
            assert rows[connected.entity_id]["integration_name"] == "Garage charger"
            assert "state" not in rows[connected.entity_id]
            assert len(rows) > 1024
            HACatalogue().receive(body, dt_util.utcnow())

    asyncio.run(scenario())


def test_unconfigured_live_installation_creates_no_fictitious_battery():
    """A production app before setup exposes only EMS/grid devices. Missing battery configuration must not invent battery entities."""
    from homeassistant.helpers import device_registry as dr

    async def scenario():
        """Use the production basis path and actual HA registry, without a synthetic legacy device catalogue."""
        async with running_chain(live=True, fixture_devices=False) as (hass, entry, _url, _root):
            assert entry.runtime_data.devices == []
            devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
            assert {device.name for device in devices} == {"Open Energy Planner", "Grid"}
            assert not any("battery" in state.entity_id for state in hass.states.async_all())

    asyncio.run(scenario())


def test_related_setup_sensors_exclude_writable_number_entities():
    """A number setpoint on the grid device must never become a measurement suggestion or invalidate the sensor-only catalogue."""
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from custom_components.open_energy_planner.catalogue import setup_catalogue
    from open_energy_planner.home_assistant.catalogue import HACatalogue

    async def scenario():
        """Exercise the genuine registries with a misleading W-valued control sharing the meter's device."""
        async with running_chain(live=True, recorder=True) as (hass, entry, _url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await setup_energy(hass)
            device = dr.async_get(hass).async_get_or_create(
                config_entry_id=entry.entry_id, identifiers={("test", "meter")}
            )
            registry = er.async_get(hass)
            meter = registry.async_get_or_create(
                "sensor", "test", "meter", suggested_object_id="meter", device_id=device.id
            )
            control = registry.async_get_or_create(
                "number", "test", "setpoint", suggested_object_id="setpoint", device_id=device.id
            )
            hass.states.async_set(
                meter.entity_id,
                100,
                {
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                },
            )
            hass.states.async_set(
                control.entity_id, 500, {"unit_of_measurement": "W", "device_class": "power"}
            )
            manager = await async_get_manager(hass)
            await manager.async_update(
                {
                    "energy_sources": [{"type": "grid", "stat_energy_from": meter.entity_id}],
                    "device_consumption": [],
                }
            )
            body = await setup_catalogue(hass)
            assert body["suggestions"]["grid_power"] is None
            assert all(
                entity_item["entity_id"].startswith("sensor.") for entity_item in body["entities"]
            )
            HACatalogue().receive(body, dt_util.utcnow())

    asyncio.run(scenario())


def test_energy_device_names_follow_sources_preserve_user_names_and_identities():
    """Real HA uses Energy-friendly defaults, updates them in place and preserves a user's device rename across updates."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from custom_components.open_energy_planner.catalogue import setup_catalogue
    from custom_components.open_energy_planner.entity import source_device_name

    async def scenario():
        """Exercise both source types and registry-only fallback without any physical service calls."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            definitions = [
                {
                    "id": "battery_opaque",
                    "kind": "battery",
                    "name": "battery_opaque",
                    "energy_entity": "sensor.battery_energy",
                    "configured": False,
                },
                {
                    "id": "solar_opaque",
                    "kind": "solar",
                    "name": "solar_opaque",
                    "energy_entity": "sensor.pv_energy",
                    "configured": False,
                },
            ]
            for device, name in zip(definitions, ("Garage battery", "South roof"), strict=True):
                hass.states.async_set(device["energy_entity"], "unknown", {"friendly_name": name})
            coordinator.devices = definitions
            coordinator.refresh(dt_util.utcnow())
            await hass.async_block_till_done()
            registry = dr.async_get(hass)
            devices = [
                registry.async_get_device_by_identifier(
                    (DOMAIN, f"{entry.entry_id}_unit_{device_item['id']}"), entry.entry_id
                )
                for device_item in definitions
            ]
            assert [device_item.name for device_item in devices] == ["Garage battery", "South roof"]
            entity_registry = er.async_get(hass)
            before = {
                entity_item.unique_id: (entity_item.entity_id, entity_item.device_id)
                for entity_item in er.async_entries_for_config_entry(
                    entity_registry, entry.entry_id
                )
            }
            from homeassistant.components.energy.data import async_get_manager

            manager = await async_get_manager(hass)
            manager.data = {
                "energy_sources": [
                    {
                        "type": "solar",
                        "stat_energy_from": "sensor.pv_energy",
                        "name": "Dashboard roof",
                    }
                ]
            }
            registry.async_update_device(devices[0].id, name_by_user="My battery")
            hass.states.async_set("sensor.battery_energy", "0", {"friendly_name": "Renamed source"})
            coordinator.refresh(dt_util.utcnow())
            await hass.async_block_till_done()
            assert registry.async_get(devices[0].id).name == "Renamed source"
            assert registry.async_get(devices[0].id).name_by_user == "My battery"
            assert registry.async_get(devices[1].id).name == "Dashboard roof"
            after = {
                entity_item.unique_id: (entity_item.entity_id, entity_item.device_id)
                for entity_item in er.async_entries_for_config_entry(
                    entity_registry, entry.entry_id
                )
            }
            assert before == after
            physical = registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={("fixture", "physical-battery")},
                name="Test battery",
            )
            hass.states.async_remove("sensor.battery_energy")
            source_entry = entity_registry.async_get_or_create(
                "sensor",
                "fixture",
                "battery-source",
                device_id=physical.id,
                suggested_object_id="battery_energy",
                original_name="Test battery discharge",
            )
            assert source_entry.entity_id == "sensor.battery_energy"
            assert source_entry.device_id == physical.id
            hass.states.async_set(
                "sensor.battery_energy", "0", {"friendly_name": "Test battery discharge"}
            )
            manager.data["energy_sources"].append(
                {
                    "type": "battery",
                    "stat_energy_from": "sensor.battery_energy",
                    "name": "Dashboard battery discharge",
                }
            )
            coordinator.refresh(dt_util.utcnow())
            await hass.async_block_till_done()
            assert registry.async_get(devices[0].id).name == "Dashboard battery discharge"
            assert registry.async_get(devices[0].id).name_by_user == "My battery"
            registry.async_update_device(physical.id, name_by_user="Home battery")
            coordinator.refresh(dt_util.utcnow())
            await hass.async_block_till_done()
            assert registry.async_get(devices[0].id).name == "Dashboard battery discharge"
            assert registry.async_get(devices[0].id).name_by_user == "My battery"
            assert before == {
                entity_item.unique_id: (entity_item.entity_id, entity_item.device_id)
                for entity_item in er.async_entries_for_config_entry(
                    entity_registry, entry.entry_id
                )
            }
            metadata = await setup_catalogue(hass)
            names = {row["entity_id"]: row for row in metadata["entities"]}
            assert names["sensor.battery_energy"]["energy_name"] == "Dashboard battery discharge"
            assert names["sensor.pv_energy"]["energy_name"] == "Dashboard roof"
            manager.data["energy_sources"][-1]["name"] = "Yixiang MB56 32kWh"
            coordinator.refresh(dt_util.utcnow())
            await hass.async_block_till_done()
            assert registry.async_get(devices[0].id).name == "Yixiang MB56 32kWh"
            hass.states.async_remove("sensor.pv_energy")
            entity_registry.async_get_or_create(
                "sensor",
                "fixture",
                "pv-source",
                suggested_object_id="pv_energy",
                original_name="Registered roof",
            )
            assert source_device_name(hass, definitions[1]) == "Registered roof"
            assert (
                source_device_name(
                    hass,
                    definitions[1],
                    [
                        {
                            "type": "solar",
                            "stat_energy_from": "sensor.pv_energy",
                            "name": " sensor.pv_energy ",
                        }
                    ],
                )
                == "Registered roof"
            )
            assert (
                source_device_name(hass, {"id": "opaque", "kind": "solar", "name": " opaque "})
                == "Solar"
            )
            assert (
                source_device_name(hass, {"id": "opaque", "kind": "solar", "name": " Roof "})
                == "Roof"
            )
            assert source_device_name(hass, {"id": "opaque", "kind": "battery"}) == "Battery"
            assert source_device_name(hass, {"id": "opaque", "kind": "solar"}) == "Solar"

    asyncio.run(scenario())


def test_energy_consumer_discovery_deduplicates_configured_meter_ids():
    """Duplicate Energy entries retain one option and its first upstream link rather than rejecting the whole catalogue."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from custom_components.open_energy_planner.catalogue import energy_suggestions

    async def scenario():
        """Read a manager fixture without requiring any live sensor states or historical measurements."""
        data = {
            "energy_sources": [{"type": "grid", "stat_energy_from": "sensor.import"}],
            "device_consumption": [
                {"stat_consumption": "sensor.heat", "stat_rate": "sensor.heat_power"},
                {"stat_consumption": "sensor.heat", "included_in_stat": "sensor.other"},
            ],
        }
        with patch(
            "homeassistant.components.energy.data.async_get_manager",
            AsyncMock(return_value=SimpleNamespace(data=data)),
        ):
            result = await energy_suggestions(object())
        assert result["consumers"] == [
            {"entity_id": "sensor.heat", "upstream": None, "power_entity": "sensor.heat_power"}
        ]

    asyncio.run(scenario())


def test_two_active_energy_instances_have_independent_http_listeners():
    """Both real HA instances must enable Energy while the other HTTP listener remains active."""
    from homeassistant.auth import auth_manager_from_config

    async def scenario():
        """A shared default port would make the second genuine component setup fail."""
        async with (
            running_chain(recorder=True) as (first_hass, *_),
            running_chain(recorder=True) as (second_hass, *_),
        ):
            for hass in (first_hass, second_hass):
                hass.auth = await auth_manager_from_config(hass, [], [])
                assert await setup_energy(hass)

    asyncio.run(scenario())
