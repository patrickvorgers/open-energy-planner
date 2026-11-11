"""Installation size must not silently discard sources or impose arbitrary device counts."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from open_energy_planner.appliances.ev.configuration import configuration
from open_energy_planner.appliances.forecasting import parse_selection
from open_energy_planner.demand.calculation.model import (
    DemandConfiguration,
    EnergyTerm,
    ExcludedLoad,
)
from open_energy_planner.home_assistant.catalogue import HACatalogue
from open_energy_planner.solar.calculation.model import (
    SolarConfiguration,
    SolarInverter,
    SolarSource,
)
from tests.ha_contract.support.metadata import setup_metadata


def test_settlement_retains_a_serialized_resource_bound():
    """Many individually valid channels cannot create an unbounded settlement frame."""
    from open_energy_planner.savings.model import Frame

    now = datetime(2026, 11, 7, tzinfo=UTC)
    with pytest.raises(ValueError, match="Settlement frame exceeds size limit"):
        Frame(
            now,
            now + timedelta(minutes=15),
            {f"sensor.channel_{index}": 0.1 for index in range(12000)},
            {},
            {},
            {},
            "recorded",
        )


def test_history_preserves_forty_component_meter_endpoints():
    """The final aggregate endpoint retains every physical component above sixteen meters."""
    from open_energy_planner.history.model import EnergyHistory, EnergyHour

    now = datetime(2026, 11, 7, tzinfo=UTC)
    components = tuple((f"sensor.export_{index}", float(index)) for index in range(40))
    total = sum(value for _entity, value in components)
    result = EnergyHistory("sensor.aggregate", (EnergyHour(now, total, total),), components)
    assert result.component_meters == components


def test_statistics_and_settlement_keep_eighty_explicit_channels():
    """All requested export members and settlement deltas remain present above the former bounds."""
    from open_energy_planner.contracts.bonus_statistics import parse_statistics
    from open_energy_planner.savings.model import Frame

    now = datetime(2026, 11, 7, tzinfo=UTC)
    entities = tuple(f"sensor.export_{index}" for index in range(80))
    request = SimpleNamespace(
        entity_ids=entities, periods=(("annual", now),), end=now + timedelta(hours=1)
    )
    body = {
        "request_version": "synthetic",
        "statistics": [
            {
                "statistic_id": entity,
                "metadata_id": index + 1,
                "source": "recorder",
                "unit": "kWh",
                "hours": [[now.timestamp(), 10.0, 10.0]],
            }
            for index, entity in enumerate(entities)
        ],
    }
    assert {channel.identity.statistic_id for channel in parse_statistics(body, request)} == set(
        entities
    )
    values = {entity: 0.1 for entity in entities}
    frame = Frame(now, now + timedelta(minutes=15), values, {}, {}, {}, "recorded")
    assert frame.channels == values


def test_eight_thermal_stores_preserve_independent_physics():
    """Eight stores retain unique identities, sensors and physical bounds above the former four."""
    from open_energy_planner.appliances.thermal.service import ThermalService
    from tests.app.thermal.test_thermal_api import configuration as thermal_configuration

    prototype = thermal_configuration()["stores"][0]
    stores = []
    for index in range(8):
        store = deepcopy(prototype)
        store.update(
            id=str(UUID(int=index + 1)),
            energy_entity=f"sensor.boiler_{index}_energy",
            temperature_entity=f"sensor.boiler_{index}_temperature",
            power_entity=f"sensor.boiler_{index}_power",
        )
        stores.append(store)
    ThermalService._shape({"stores": stores})
    assert len({store["id"] for store in stores}) == 8


def test_eighty_observation_bindings_retain_required_grid_roles():
    """A larger source configuration keeps every required power channel and unique binding."""
    from open_energy_planner.home_assistant.observation_wire import parse_configuration

    bindings = [
        {
            "key": "grid.power" if index == 0 else f"grid.power.channel{index}",
            "entity_id": f"sensor.grid_{index}",
            "quantity": "power",
            "required": True,
            "multiplier": 1,
            "maximum_age_s": 300,
            "timestamp_attribute": None,
        }
        for index in range(80)
    ]
    parsed = parse_configuration({"bindings": bindings})
    assert len(parsed.bindings) == 80


def test_catalogue_preserves_sources_beyond_the_former_global_entity_limit():
    """A valid battery source after 2,000 ordinary sensors must remain discoverable."""
    body = setup_metadata()
    body["entities"].extend(
        {
            "entity_id": f"sensor.site_{index:05}",
            "name": f"Site sensor {index}",
            "device_name": f"Device {index}",
            "device_id": f"device_{index}",
            "unit": "%",
            "device_class": "battery",
            "state_class": "measurement",
        }
        for index in range(2000)
    )
    catalogue = HACatalogue()
    catalogue.receive(body, datetime.now(UTC))
    assert len(catalogue.value["entities"]) == 2002
    assert catalogue.value["entities"][-1]["entity_id"] == "sensor.site_01999"


def test_demand_accepts_more_than_sixteen_disjoint_channels_and_exclusions():
    """Every channel keeps its own sign and identity above the former count limit."""
    terms = tuple(EnergyTerm(f"sensor.import_{index}", 1) for index in range(40))
    exclusions = tuple(
        ExcludedLoad(f"device_{index}", f"sensor.load_{index}") for index in range(40)
    )
    value = DemandConfiguration("Europe/Amsterdam", 3500, terms, exclusions)
    assert value.terms == terms and value.exclusions == exclusions


def test_solar_accepts_more_than_sixteen_independent_sources_and_inverters():
    """All disjoint meters retain their own physical inverter limits."""
    sources = tuple(
        SolarSource(f"roof_{index}", f"sensor.pv_{index}", f"inverter_{index}", 2000, "south")
        for index in range(40)
    )
    inverters = tuple(SolarInverter(f"inverter_{index}", 3000) for index in range(40))
    value = SolarConfiguration(sources, inverters)
    assert value.sources == sources and value.inverters == inverters


def test_consumer_selection_preserves_all_independent_energy_meters():
    """A larger selection keeps explicit weather sensitivity instead of dropping later devices."""
    selected = [
        {"entity_id": f"sensor.load_{index:03}", "weather_sensitive": False} for index in range(40)
    ]
    assert parse_selection(selected) == tuple(selected)


def test_ev_configuration_accepts_twenty_vehicles_and_eight_disjoint_charge_points():
    """Counts do not replace unique sources, Energy ownership or physical charging limits."""
    vehicles = [
        {
            "id": str(UUID(int=index + 1)),
            "soc_entity": f"sensor.car_{index}",
            "capacity_kwh": 60,
            "maximum_w": 11000,
            "target_soc": 80,
            "departure_time": "07:00",
        }
        for index in range(20)
    ]
    points = [
        {
            "id": str(UUID(int=index + 100)),
            "energy_entity": f"sensor.charge_{index}",
            "power_entity": f"sensor.charge_power_{index}",
            "connected_entity": f"binary_sensor.connected_{index}",
            "minimum_w": 1400,
            "maximum_w": 11000,
            "control": "variable",
            "vehicle_ids": [vehicles[index]["id"]],
        }
        for index in range(8)
    ]
    entities = [
        {"entity_id": vehicle["soc_entity"], "unit": "%", "device_class": "battery"}
        for vehicle in vehicles
    ]
    for point in points:
        entities.extend(
            [
                {"entity_id": point["power_entity"], "unit": "W", "device_class": "power"},
                {"entity_id": point["connected_entity"]},
            ]
        )
    consumers = SimpleNamespace(
        selected=[],
        options=[
            {
                "entity_id": point["energy_entity"],
                "power_entity": point["power_entity"],
                "upstream": None,
            }
            for point in points
        ],
    )
    result, selected = configuration(
        {"vehicles": vehicles, "points": points},
        {"entities": entities},
        consumers,
        SimpleNamespace(state={"devices": {}}),
    )
    assert len(result["vehicles"]) == 20 and result["points"] == points
    assert {source["entity_id"] for source in selected} == {
        point["energy_entity"] for point in points
    }
