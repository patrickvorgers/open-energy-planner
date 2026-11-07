"""Keep large typed source and consumer projections usable by the real HA adapter."""

import asyncio
from datetime import UTC, datetime

import pytest

from custom_components.open_energy_planner.consumer_sensors import validate
from custom_components.open_energy_planner.observation_bridge import ObservationBridge
from tests.custom_component.support.chain import running_chain


def test_eighty_settlement_channels_use_bounded_recorder_queries(monkeypatch):
    """All eighty channels reach the frame while each Recorder call handles at most sixty-four channels."""
    from datetime import timedelta

    from custom_components.open_energy_planner import settlement

    async def scenario():
        """Provide independent aligned cumulative sums through real Recorder executor ownership."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
                hours=2
            )
            entities = [f"sensor.grid_import_{index}" for index in range(80)]
            queried = []

            def metadata(_hass, statistic_ids):
                """Require bounded metadata lookups for valid synthetic energy sum channels."""
                assert len(statistic_ids) <= 64
                return {
                    entity: (index, {"has_sum": True, "unit_of_measurement": "kWh"})
                    for index, entity in enumerate(sorted(statistic_ids), 1)
                }

            def statistics(_hass, _start, _end, statistic_ids, *_arguments):
                """Each five-minute endpoint rises by one kWh, making the quarter exactly three."""
                assert len(statistic_ids) <= 64
                queried.extend(statistic_ids)
                return {
                    entity: [
                        {
                            "start": (
                                start - timedelta(minutes=5) + timedelta(minutes=5 * index)
                            ).timestamp(),
                            "sum": 100 + index,
                            "state": 100 + index,
                        }
                        for index in range(4)
                    ]
                    for entity in statistic_ids
                }

            monkeypatch.setattr(settlement, "get_metadata", metadata)
            monkeypatch.setattr(settlement, "statistics_during_period", statistics)
            result = await settlement.SettlementBridge(hass).capture(
                {
                    "version": "synthetic-large-settlement",
                    "start": start.isoformat(),
                    "end": (start + timedelta(minutes=15)).isoformat(),
                    "bindings": {
                        "terms": [{"entity_id": entity, "sign": 1} for entity in entities],
                        "imports": entities,
                        "exports": [],
                        "solar": [],
                        "batteries": [],
                    },
                }
            )
            assert result is not None
            assert set(queried) == set(entities)
            assert result["frames"][0]["channels"] == {entity: 3.0 for entity in entities}

    asyncio.run(scenario())


def test_eighty_history_meters_use_bounded_recorder_queries(monkeypatch):
    """All eighty endpoints survive while each Recorder call handles at most sixty-four channels."""
    from datetime import timedelta

    from custom_components.open_energy_planner import history

    async def scenario():
        """Keep real HA/Recorder lifecycle and replace only independent historical sample responses."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            now = datetime(2026, 11, 7, tzinfo=UTC)
            queried = []

            def metadata(_hass, statistic_ids):
                """Expose valid kWh sum channels for each explicitly requested batch."""
                assert len(statistic_ids) <= 64
                return {
                    entity: (index, {"has_sum": True, "unit_of_measurement": "kWh"})
                    for index, entity in enumerate(sorted(statistic_ids), 1)
                }

            def statistics(_hass, _start, _end, statistic_ids, *_arguments):
                """Return equal, aligned physical endpoints without omitting any requested meter."""
                assert len(statistic_ids) <= 64
                queried.extend(statistic_ids)
                return {
                    entity: [{"start": now.timestamp(), "sum": 10.0, "state": 10.0}]
                    for entity in statistic_ids
                }

            monkeypatch.setattr(history, "get_metadata", metadata)
            monkeypatch.setattr(history, "statistics_during_period", statistics)
            entities = [f"sensor.export_{index}" for index in range(80)]
            result = await history.HistoryBridge(hass).capture(
                {
                    "version": "synthetic-large-history",
                    "entity_id": "sensor.aggregate",
                    "entity_ids": entities,
                    "start": now.isoformat(),
                    "end": (now + timedelta(hours=1)).isoformat(),
                }
            )
            assert set(queried) == set(entities)
            assert len(queried) == 80
            assert result["hours"] == [[now.timestamp(), 800.0, 800.0]]
            assert {entity for entity, _value in result["component_meters"]} == set(entities)

    asyncio.run(scenario())


def test_forty_solar_planes_preserve_geometry_and_physical_capacity():
    """A supported provider retains every declared plane instead of rejecting its seventeenth."""
    from types import SimpleNamespace

    from custom_components.open_energy_planner.solar_adapters import forecast_solar_configuration

    planes = [
        SimpleNamespace(
            subentry_id=f"plane{index}",
            title=f"Plane {index}",
            data={"modules_power": 400, "azimuth": 180, "declination": 30},
        )
        for index in range(40)
    ]
    entry = SimpleNamespace(
        entry_id="synthetic-provider",
        title="Roof",
        options={},
        get_subentries_of_type=lambda _kind: planes,
    )
    result = forecast_solar_configuration(entry)
    assert len(result["panels"]) == 40
    assert sum(panel["capacity_w"] for panel in result["panels"]) == 16000


def test_decoded_projection_bound_rejects_unbounded_callers():
    """Direct receiver calls retain the byte bound even without the streaming HTTP boundary."""
    from custom_components.open_energy_planner.wire_limits import validate_projection_size

    validate_projection_size({"devices": [{"id": f"device{index}"} for index in range(80)]}, 262144)
    with pytest.raises(ValueError, match="App projection exceeds size limit"):
        validate_projection_size({"padding": "x" * 262145}, 262144)


@pytest.mark.parametrize("path,byte_limit", [("/v1/ev", 262144), ("/v1/snapshot", 4194304)])
def test_http_resource_limits_remain_enforced_for_large_projections(path, byte_limit):
    """Larger device support never permits an unbounded streaming HTTP response."""
    from aiohttp import ClientSession, web
    from aiohttp.test_utils import TestServer

    from custom_components.open_energy_planner.client import LocalClient
    from custom_components.open_energy_planner.const import BUILD_INFO

    async def scenario():
        """Exercise the actual response reader using a server beyond the independent byte bound."""

        async def oversized(_request):
            """Supply only synthetic bytes and the required matching protocol identity."""
            return web.json_response(
                {"padding": "x" * (byte_limit + 1)},
                headers={
                    "X-OEP-Release": BUILD_INFO["version"],
                    "X-OEP-Build": BUILD_INFO["build"] or "",
                },
            )

        application = web.Application()
        application.router.add_get(path, oversized)
        async with TestServer(application) as server, ClientSession() as session:
            client = LocalClient(session, str(server.make_url("/")), "synthetic-token")
            with pytest.raises(ValueError, match="App response exceeds size limit"):
                await client.request("GET", path)

    asyncio.run(scenario())


def test_forty_virtual_devices_survive_coordinator_validation(monkeypatch):
    """Forty solar or battery identities survive synchronization without fabricating commands."""

    async def scenario():
        """Keep real app requests and replace only the independent accepted device fixture."""
        async with running_chain(live=True) as (_hass, entry, _url, _root):
            coordinator = entry.runtime_data
            request = coordinator.client.request
            devices = [
                {"id": f"synthetic_roof_{index}", "kind": "solar", "configured": False}
                for index in range(40)
            ]

            async def with_devices(method, path, body=None):
                """Retain HTTP authority and inject a complete independently specified membership."""
                result = await request(method, path, body)
                if "installation" in result:
                    result["installation"]["devices"] = devices
                return result

            monkeypatch.setattr(coordinator.client, "request", with_devices)
            await coordinator.sync()
            assert coordinator.devices == devices

    asyncio.run(scenario())


def test_twenty_vehicles_and_eight_charge_points_reach_ha_projection():
    """The HA receiver keeps every configured car and distinct physical charge point."""
    from custom_components.open_energy_planner.ev import receive

    vehicles = [
        {
            "id": f"car{index}",
            "name": f"Family car {index}",
            "soc_entity": f"sensor.car{index}_battery",
            "capacity_kwh": 60,
            "maximum_w": 11000,
            "target_soc": 80,
            "departure_time": "07:00",
            "next_departure": "2026-11-07T07:00:00Z",
        }
        for index in range(20)
    ]
    points = [
        {
            "id": f"charger{index}",
            "name": f"Charge point {index}",
            "energy_entity": f"sensor.charger{index}_energy",
            "power_entity": f"sensor.charger{index}_power",
            "connected_entity": f"binary_sensor.charger{index}_connected",
            "minimum_w": 1400,
            "maximum_w": 11000,
            "control": "variable",
            "vehicle_ids": [vehicles[index]["id"]],
            "actual": {},
        }
        for index in range(8)
    ]
    document = {
        "configuration_version": "synthetic-config",
        "revision": "synthetic-revision",
        "vehicles": vehicles,
        "points": points,
    }
    assert receive(document) == document


def test_eight_thermal_devices_reach_ha_projection():
    """The HA receiver retains all boilers with independently validated temperatures and sources."""
    from custom_components.open_energy_planner.thermal import receive
    from tests.custom_component.integration.thermal.test_thermal import device

    devices = []
    for index in range(8):
        row = device(datetime.now(UTC))
        row.update(
            id=f"tank{index}",
            energy_entity=f"sensor.tank{index}_energy",
            temperature_entity=f"sensor.tank{index}_temperature",
            power_entity=f"sensor.tank{index}_power",
        )
        devices.append(row)
    document = {"configuration_version": "b" * 64, "revision": "a" * 64, "devices": devices}
    assert len(receive(document)["devices"]) == 8


def test_large_consumer_catalogue_and_selection_retain_all_meters():
    """All 1,100 options and forty selected consumers remain available without truncation."""
    options = [
        {"entity_id": f"sensor.load_{index:04}", "name": f"Synthetic consumer {index}"}
        for index in range(1100)
    ]
    selected = [
        {"entity_id": option["entity_id"], "weather_sensitive": False} for option in options[-40:]
    ]
    document = {"options": options, "selected": selected, "result": None}
    assert validate(document) == document


def test_real_ha_observation_bridge_keeps_eighty_distinct_power_channels():
    """Every selected channel survives adapter validation and contributes its own reading."""

    async def scenario():
        """Use isolated HA states without changing an installed device or running its hardware."""
        async with running_chain(live=True) as (hass, *_):
            bindings = []
            for index in range(80):
                entity = f"sensor.synthetic_grid_{index}"
                hass.states.async_set(entity, str(index), {"unit_of_measurement": "W"})
                bindings.append(
                    {
                        "key": "grid.power" if index == 0 else f"grid.power.channel{index}",
                        "entity_id": entity,
                        "quantity": "power",
                        "required": True,
                        "multiplier": 1,
                        "maximum_age_s": 300,
                        "timestamp_attribute": None,
                    }
                )
            bridge = ObservationBridge(
                hass.states, {"revision": "synthetic-revision", "bindings": bindings}
            )
            ready, report = bridge.capture(datetime.now(UTC))
            assert ready
            assert len(report["readings"]) == 80
            assert {reading["entity_id"] for reading in report["readings"]} == {
                binding["entity_id"] for binding in bindings
            }

    asyncio.run(scenario())
