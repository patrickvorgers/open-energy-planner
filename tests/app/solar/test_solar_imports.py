"""Retain exact imported panel geometry and independent manual configuration provenance."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from custom_components.open_energy_planner.protocol import validate_device_configuration
from open_energy_planner.installation.api import catalogue
from open_energy_planner.solar.calculation.model import (
    SolarConfiguration,
    SolarImport,
    SolarInverter,
    SolarPanel,
    SolarSource,
    version,
)
from open_energy_planner.solar.calculation.predictor import PVWattsPredictor
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.wire import document, parse_configuration
from tests.app.solar.support import SITE, plant, weather
from tests.database.support.archive import make_store


@pytest.mark.parametrize("choice", ["automatic", "pvwatts", "ha", "ml"])
@pytest.mark.parametrize("exact_geometry", [True, False])
def test_imported_solar_settings_preserve_the_ha_device_contract(choice, exact_geometry):
    """Exact planes cannot interrupt HA synchronization or expose unused coarse geometry as confirmed settings."""
    panel = SolarPanel("east", 3000, 90, 35)
    source = SolarSource(
        "roof",
        "sensor.roof",
        "inverter",
        3000,
        "west",
        tilt=45,
        panels=(panel,) if exact_geometry else (),
        ha_imports=(SolarImport("forecast_solar", "entry", (panel,)),),
        forecast_source=choice,
    )
    configuration = SolarConfiguration((source,), (SolarInverter("inverter", 2500),))
    devices = catalogue(
        SimpleNamespace(current=SimpleNamespace(batteries=())),
        SimpleNamespace(configuration=configuration),
    )
    assert len(devices) == 1
    validate_device_configuration(devices[0])
    assert devices[0]["energy_entity"] == "sensor.roof"
    assert devices[0]["configuration"]["capacity_w"] == 3000
    assert devices[0]["configuration"]["inverter"]["ac_limit_w"] == 2500
    assert devices[0]["configuration"]["orientation"] == (None if exact_geometry else "west")
    assert devices[0]["configuration"]["tilt"] == (None if exact_geometry else 45)
    assert devices[0]["can_curtail"] is False


def test_metered_group_preserves_distinct_planes_and_one_inverter_limit():
    """One east/west metered group equals two independent physical planes under the same single AC cap."""
    panels = (SolarPanel("east", 1000, 90, 35), SolarPanel("west", 2000, 270, 50))
    inverter = SolarInverter("shared", 500)
    grouped = SolarConfiguration(
        (SolarSource("group", "sensor.group", "shared", 3000, "south", panels=panels),),
        (inverter,),
    )
    reference = SolarConfiguration(
        (
            SolarSource("east", "sensor.east", "shared", 1000, "east", tilt=35),
            SolarSource("west", "sensor.west", "shared", 2000, "west", tilt=50),
        ),
        (inverter,),
    )
    predictor = PVWattsPredictor()
    expected = predictor.hourly(reference, SITE, weather())
    for actual in predictor.hourly(grouped, SITE, weather()):
        independent = [row for row in expected if row.start == actual.start]
        assert actual.dc_kwh == pytest.approx(sum(row.dc_kwh for row in independent))
        assert actual.ac_kwh == pytest.approx(sum(row.ac_kwh for row in independent))
        assert actual.ac_kwh <= 0.5


def test_manual_panel_override_preserves_original_import_after_restart(tmp_path):
    """Editing OEP geometry changes only its saved model and retains the original provider snapshot."""
    original = SolarPanel("plane", 3410, 205, 45, name="HA south roof")
    provider = SolarImport("forecast_solar", "entry", (original,), 12000)
    edited = replace(original, capacity_w=3300, azimuth=210, name="House roof")
    configuration = SolarConfiguration(
        (
            SolarSource(
                "pv",
                "sensor.pv",
                "inverter",
                3300,
                "south",
                panels=(edited,),
                ha_imports=(provider,),
            ),
        ),
        (SolarInverter("inverter", 12000),),
    )
    store = make_store(tmp_path, SolarStore)
    store.save(parse_configuration(document(configuration)))
    restored = SolarStore(store.documents)
    assert restored.issue is None
    assert restored.current == configuration
    assert restored.current.sources[0].geometry[0].azimuth == 210
    assert restored.current.sources[0].ha_imports[0].panels[0].azimuth == 205
    assert restored.current.sources[0].ha_imports[0].panels[0].capacity_w == 3410
    assert restored.current.sources[0].panels[0].name == "House roof"
    assert restored.current.sources[0].ha_imports[0].panels[0].name == "HA south roof"


def test_unnamed_planes_keep_archived_versions_and_names_preserve_accuracy_family(tmp_path):
    """Old panel archives still load, and changing a label cannot discard source accuracy evidence."""
    from open_energy_planner.solar.source_selection import family

    panel = SolarPanel("plane", 3000, 180, 30)
    source = replace(
        plant().sources[0],
        capacity_w=3000,
        panels=(panel,),
        ha_imports=(SolarImport("forecast_solar", "entry", (panel,)),),
    )
    configuration = replace(plant(), sources=(source,))
    original = document(configuration)
    for group in (original["sources"][0], *original["sources"][0]["ha_imports"]):
        for plane in group["panels"]:
            plane.pop("name")
    original["sources"][0].pop("forecast_source")
    original["sources"][0].pop("unrestricted_history_start")
    original["sources"][0].pop("unrestricted_history_end")
    original["sources"][0].pop("learning_start")
    assert configuration.version == version(original)
    assert parse_configuration(original) == configuration
    archived = {**original, "version": configuration.version}
    archived["sources"][0]["forecast_source"] = "automatic"
    store = make_store(tmp_path, SolarStore)
    store.documents.write("configuration-" + configuration.version + ".json", archived)
    store.documents.write("current.json", configuration.version)
    restored = SolarStore(store.documents)
    assert restored.issue is None
    assert restored.current == configuration
    restored.save(configuration)
    assert restored.documents.read("configuration-" + configuration.version + ".json") == archived
    named = replace(
        source,
        panels=(replace(panel, name="South roof"),),
        ha_imports=(SolarImport("forecast_solar", "entry", (replace(panel, name="HA roof"),)),),
    )
    assert family(configuration, named, SITE) == family(configuration, source, SITE)
    assert replace(configuration, sources=(named,)).version != configuration.version


@pytest.mark.parametrize("name", [None, 42, "x" * 201])
def test_panel_names_reject_invalid_wire_values(name):
    """Display labels accept bounded text only, independently of stable plane identities."""
    with pytest.raises(ValueError, match="name"):
        SolarPanel("plane", 1000, 180, 30, name=name)


def test_existing_saved_manual_configuration_keeps_its_version(tmp_path):
    """Optional geometry metadata neither invalidates nor rewrites an unchanged saved manual configuration."""
    configuration = plant()
    original = asdict(configuration)
    for source in original["sources"]:
        source.pop("panels")
        source.pop("ha_imports")
        source.pop("forecast_source")
        source.pop("unrestricted_history_start")
        source.pop("unrestricted_history_end")
        source.pop("learning_start")
    original_ref = version(original)
    assert configuration.version == original_ref
    store = make_store(tmp_path, SolarStore)
    store.documents.write(
        "configuration-" + original_ref + ".json", {**original, "version": original_ref}
    )
    store.documents.write("current.json", original_ref)
    restored = SolarStore(store.documents)
    assert restored.current == configuration
    assert restored.issue is None


@pytest.mark.parametrize("azimuth", [True, "205", -1, 361, float("nan"), 10**309])
def test_imported_plane_geometry_rejects_malformed_angles(azimuth):
    """Imported geometry remains bounded numeric data rather than guessed or coerced metadata."""
    with pytest.raises(ValueError):
        SolarPanel("plane", 1000, azimuth, 35)


def test_plane_capacity_cannot_silently_disagree_with_metered_group():
    """A partial provider import cannot silently omit capacity from the modelled production group."""
    with pytest.raises(ValueError, match="capacities"):
        replace(plant().sources[0], panels=(SolarPanel("one", 500, 180, 30),))


def test_import_provenance_rejects_credentials():
    """Only normalized geometry and source identities enter durable OEP configuration."""
    body = document(plant())
    body["sources"][0]["ha_imports"] = [
        {
            "adapter": "forecast_solar",
            "entry_id": "entry",
            "panels": [{"panel_id": "plane", "capacity_w": 1000, "azimuth": 180, "tilt": 30}],
            "api_key": "must-not-be-stored",
        }
    ]
    with pytest.raises(TypeError, match="api_key"):
        parse_configuration(body)
