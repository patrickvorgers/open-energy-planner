"""Energy naming changes presentation without changing identities, models or historical evidence."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from open_energy_planner.appliances.forecasting import ConsumerForecasts
from open_energy_planner.demand.storage import DemandStore
from open_energy_planner.home_assistant.catalogue import HACatalogue
from open_energy_planner.home_assistant.names import display_plan, source_name
from tests.app.demand.test_consumers import catalogue as consumer_catalogue
from tests.database.support.archive import make_store
from tests.ha_contract.test_energy_authority import energy_installation


@pytest.mark.parametrize(
    "metadata,expected",
    [
        (
            {"energy_name": "  Yixiang MB56 32kWh  ", "device_name": "Deye", "name": "Discharge"},
            "Yixiang MB56 32kWh",
        ),
        ({"energy_name": " ", "device_name": "Deye", "name": "Discharge"}, "Deye"),
        ({"name": "Discharge"}, "Discharge"),
        ({"entity_id": "sensor.battery", "name": "sensor.battery"}, "Battery"),
    ],
)
def test_energy_name_precedes_device_sensor_and_generic(metadata, expected):
    """Resolve explicit Energy, device, sensor and generic labels in that order."""
    assert source_name(metadata, "Battery") == expected


@pytest.mark.parametrize("invalid", [None, True, 42, "x" * 161])
def test_energy_name_remains_bounded_text_at_admission(invalid):
    """Reject invalid display metadata without replacing the last accepted catalogue."""
    metadata, _settings = energy_installation()
    catalogue = HACatalogue()
    catalogue.receive(metadata, datetime.now(UTC))
    invalid_metadata = deepcopy(metadata)
    invalid_metadata["entities"][0]["energy_name"] = invalid
    with pytest.raises(ValueError, match="metadata"):
        catalogue.receive(invalid_metadata, datetime.now(UTC))
    assert catalogue.value == metadata


def test_rename_projects_current_and_archived_names_without_mutation():
    """A renamed battery retains its ID/configuration and old plan bytes. Removed sources keep archived names."""
    metadata, _settings = energy_installation()
    entry = next(row for row in metadata["entities"] if row["entity_id"] == "sensor.discharge")
    entry.update(energy_name="Yixiang MB56 32kWh", device_name="Deye")
    catalogue = HACatalogue()
    catalogue.receive(metadata, datetime.now(UTC))
    configured = [
        {
            "id": "battery",
            "kind": "battery",
            "name": "Old name",
            "energy_entity": "sensor.discharge",
            "configuration": {"capacity_kwh": 32},
        }
    ]
    original_devices = deepcopy(configured)
    assert catalogue.devices(configured)[0] == {**configured[0], "name": "Yixiang MB56 32kWh"}
    assert configured == original_devices
    original = {
        "plan_id": "frozen-plan",
        "input_hash": "original-input-hash",
        "input_snapshot": {"problem": {"price": 0.21}},
        "comparison_names": {"battery": "Old battery", "removed": "Last known roof"},
        "comparison_bindings": {
            "batteries": [{"device_id": "battery", "energy_entity": "sensor.discharge"}],
            "solar": [{"source_id": "removed", "entity_id": "sensor.removed"}],
        },
        "forecast_quality_names": {"sensor.discharge": "Old battery"},
    }
    before = deepcopy(original)
    view = display_plan(original, metadata)
    assert view["comparison_names"] == {
        "battery": "Yixiang MB56 32kWh",
        "removed": "Last known roof",
    }
    assert view["forecast_quality_names"]["sensor.discharge"] == "Yixiang MB56 32kWh"
    assert view["input_snapshot"] == original["input_snapshot"]
    metadata["suggestions"]["terms"] = []
    metadata["suggestions"]["batteries"] = []
    assert display_plan(original, metadata)["comparison_names"]["battery"] == "Old battery"
    assert original == before


def test_consumer_rename_retains_version_and_learned_forecast(tmp_path):
    """A display-only rename must not invalidate an existing consumer forecast or settings draft."""
    service = ConsumerForecasts(make_store(tmp_path, DemandStore).documents)
    metadata = consumer_catalogue()
    service.sync(metadata)
    version = service.version
    result = object()
    service.result = result
    metadata["entities"][0]["energy_name"] = "Living room heat pump"
    service.sync(metadata)
    assert service.options[0]["name"] == "Living room heat pump"
    assert service.version == version
    assert service.result is result


def test_daily_capture_source_links_resolve_a_current_name():
    """A frozen daily source link updates its displayed title without changing the original context."""
    context = {
        "display_sources": {"roof": "sensor.roof"},
        "comparison_names": {"roof": "Original roof"},
    }
    metadata = {"entities": [{"entity_id": "sensor.roof", "energy_name": "South roof"}]}
    assert display_plan(context, metadata)["comparison_names"]["roof"] == "South roof"
    assert context["comparison_names"]["roof"] == "Original roof"


def test_publication_names_keep_stable_ids_and_independent_fallbacks():
    """Current names decorate five device kinds while absent names retain their kind fallback."""
    from types import SimpleNamespace

    from open_energy_planner.home_assistant.names import comparison_names

    thermal = {"stores": [{"id": "tank", "energy_entity": "sensor.tank"}]}
    ev = {
        "vehicles": [{"id": "car", "soc_entity": "sensor.car"}],
        "points": [{"id": "charger", "energy_entity": "sensor.charger"}],
    }
    installation = SimpleNamespace(
        batteries=(SimpleNamespace(device_id="battery", energy_entity="sensor.battery"),)
    )
    solar = SimpleNamespace(sources=(SimpleNamespace(source_id="roof", entity_id="sensor.roof"),))
    catalogue = {"entities": [{"entity_id": "sensor.roof", "energy_name": "South roof"}]}
    assert comparison_names(catalogue, thermal, ev, installation, solar) == {
        "tank": "Thermal storage",
        "car": "Vehicle",
        "charger": "Charge point",
        "battery": "Battery",
        "roof": "South roof",
    }
    assert thermal == {"stores": [{"id": "tank", "energy_entity": "sensor.tank"}]}


def test_incomplete_balance_does_not_hide_current_battery_name():
    """Incomplete Energy setup cannot turn valid presentation metadata into an archived label."""
    metadata, _settings = energy_installation()
    entry = next(row for row in metadata["entities"] if row["entity_id"] == "sensor.discharge")
    entry["energy_name"] = "Yixiang MB56 32kWh"
    metadata["suggestions"].update(
        energy_status="incomplete", terms=[], settlement={"imports": [], "exports": []}
    )
    catalogue = HACatalogue()
    catalogue.receive(metadata, datetime.now(UTC))
    configured = [
        {
            "id": "battery",
            "kind": "battery",
            "name": "Old name",
            "energy_entity": "sensor.discharge",
        }
    ]
    assert catalogue.devices(configured) == [{**configured[0], "name": "Yixiang MB56 32kWh"}]
    assert configured[0]["name"] == "Old name"
