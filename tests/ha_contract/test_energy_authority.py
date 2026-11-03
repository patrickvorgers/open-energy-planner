"""Energy-owned meters/settings stay complete, read-only and synchronized across app boundaries."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.home_assistant.catalogue import HACatalogue
from open_energy_planner.installation.storage import parse, sources
from tests.ha_contract.support.metadata import setup_metadata


def energy_installation():
    """Declare two import/export pairs and a 10 kWh battery without any historical samples."""
    metadata = setup_metadata()
    roles = (("import2", 1), ("export", -1), ("export2", -1), ("discharge", 1), ("charge", -1))
    for name, sign in roles:
        metadata["suggestions"]["terms"].append({"entity_id": "sensor." + name, "sign": sign})
        metadata["entities"].append(
            {
                "entity_id": "sensor." + name,
                "name": name,
                "unit": "kWh",
                "device_id": name,
                "device_class": "energy",
                "state_class": "total_increasing",
            }
        )
    for name, quantity, unit in (
        ("grid2", "power", "W"),
        ("battery", "power", "W"),
        ("soc", "battery", "%"),
    ):
        metadata["entities"].append(
            {
                "entity_id": "sensor." + name,
                "name": name,
                "unit": unit,
                "device_id": name,
                "device_class": quantity,
                "state_class": "measurement",
            }
        )
    battery = {
        "energy_entity": "sensor.discharge",
        "charge_energy_entity": "sensor.charge",
        "soc_entity": "sensor.soc",
        "power_entity": "sensor.battery",
        "capacity_kwh": 10,
    }
    metadata["suggestions"].update(
        grid_power=None,
        grid_power_entities=["sensor.grid", "sensor.grid2"],
        grid_export="sensor.export",
        batteries=[battery],
        soc=["sensor.soc"],
        settlement={
            "imports": ["sensor.import", "sensor.import2"],
            "exports": ["sensor.export", "sensor.export2"],
        },
    )
    settings = {
        "grid_power_entity": "sensor.grid",
        "grid_export_entity": "sensor.export",
        "additional_grid_export_entities": ["sensor.export2"],
        "additional_grid_power_entities": ["sensor.grid2"],
        "import_limit_w": 5000,
        "export_limit_w": 5000,
        "inverters": [
            {"inverter_id": "converter", "ac_limit_w": 2000, "efficiency": 0.96, "hybrid": False}
        ],
        "batteries": [
            {
                **battery,
                "device_id": "battery",
                "inverter_id": "converter",
                "charge_limit_w": 2000,
                "discharge_limit_w": 2000,
            }
        ],
    }
    return metadata, settings


@pytest.mark.parametrize(
    "field,value",
    [
        ("capacity_kwh", 11),
        ("soc_entity", "sensor.other"),
        ("power_entity", "sensor.other"),
        ("charge_energy_entity", "sensor.other"),
        ("power_inverted", True),
    ],
)
def test_energy_battery_properties_cannot_be_overridden(field, value):
    """Changing any HA-owned battery setting fails even when the device membership still matches."""
    metadata, settings = energy_installation()
    catalogue = HACatalogue()
    catalogue.receive(metadata, datetime.now(UTC))
    catalogue.validate_membership("installation", parse(settings))
    settings["batteries"][0][field] = value
    with pytest.raises(ValueError, match="Home Assistant Energy dashboard"):
        catalogue.validate_membership("installation", parse(settings))


def test_all_grid_measurements_are_required_and_keep_separate_identities():
    """Both connections are required power readings. Omitting or inverting one cannot pass setup validation."""
    metadata, settings = energy_installation()
    catalogue = HACatalogue()
    catalogue.receive(metadata, datetime.now(UTC))
    installation = parse(settings)
    bindings = sources(installation).bindings
    grid = [binding for binding in bindings if binding.key.startswith("grid.power")]
    assert [(binding.entity_id, binding.required) for binding in grid] == [
        ("sensor.grid", True),
        ("sensor.grid2", True),
    ]
    for changed in (
        replace(installation, additional_grid_power_entities=()),
        replace(installation, grid_power_inverted=True),
    ):
        with pytest.raises(ValueError, match="Home Assistant Energy dashboard"):
            catalogue.validate_membership("installation", changed)


@pytest.mark.parametrize("source_kind", ["grid", "battery"])
def test_no_power_sensor_blocks_planning_without_hiding_energy_devices(source_kind):
    """Selecting no power sensor preserves discovered meters but rejects stale/manual power bindings at the readiness gate."""
    metadata, settings = energy_installation()
    if source_kind == "grid":
        metadata["suggestions"].update(grid_power=None, grid_power_entities=[])
    else:
        metadata["suggestions"]["batteries"][0]["power_entity"] = None
    catalogue = HACatalogue()
    catalogue.receive(metadata, datetime.now(UTC))
    assert catalogue.value["suggestions"]["energy_status"] == "ready"
    assert {term["entity_id"] for term in catalogue.value["suggestions"]["terms"]} == {
        "sensor.import",
        "sensor.import2",
        "sensor.export",
        "sensor.export2",
        "sensor.discharge",
        "sensor.charge",
    }
    assert [
        battery["energy_entity"] for battery in catalogue.value["suggestions"]["batteries"]
    ] == ["sensor.discharge"]
    with pytest.raises(ValueError, match="Home Assistant Energy dashboard"):
        catalogue.validate_membership("installation", parse(settings))


@pytest.mark.parametrize("missing", ["grid_power_entities", "capacity_kwh"])
def test_ready_catalogue_cannot_omit_authoritative_fields(missing):
    """A handcrafted ready catalogue cannot bypass ownership checks by dropping a required field."""
    metadata, _settings = energy_installation()
    if missing == "grid_power_entities":
        metadata["suggestions"].pop(missing)
    else:
        metadata["suggestions"]["batteries"][0].pop(missing)
    with pytest.raises(ValueError):
        HACatalogue().receive(metadata, datetime.now(UTC))


def test_energy_changes_reconcile_settings_and_reject_api_overrides(tmp_path):
    """A new HA capacity/SoC archives new settings automatically. Direct edits fail and original definitions survive."""

    async def scenario():
        """Exercise real catalogue, preview and save routes without external services or history."""
        token = "energy-authority-test-0123456789"
        headers = {"Authorization": "Bearer " + token}
        metadata, settings = energy_installation()
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            response = await client.post("/v1/ha/catalogue", json=metadata, headers=headers)
            assert response.status == 200, await response.text()
            setup = {
                "installation": settings,
                "solar": {"sources": [], "inverters": []},
                "demand": {
                    "annual_kwh": 3500,
                    "timezone": "Europe/Amsterdam",
                    "terms": metadata["suggestions"]["terms"],
                    "exclusions": [],
                },
            }
            assert (
                await client.post("/v1/setup/preview", json=setup, headers=headers)
            ).status == 200
            for name in ("demand", "solar", "installation"):
                response = await client.put(
                    "/v1/" + name, json={**setup[name], "expected_version": None}, headers=headers
                )
                assert response.status == 200, await response.text()
            original = (await (await client.get("/v1/setup", headers=headers)).json())[
                "installation"
            ]
            changed = deepcopy(metadata)
            changed["suggestions"]["batteries"][0]["capacity_kwh"] = 12
            changed["suggestions"]["batteries"][0]["soc_entity"] = "sensor.soc_new"
            changed["entities"].append({**changed["entities"][-1], "entity_id": "sensor.soc_new"})
            response = await client.post("/v1/ha/catalogue", json=changed, headers=headers)
            assert response.status == 200, await response.text()
            current = (await (await client.get("/v1/setup", headers=headers)).json())[
                "installation"
            ]
            assert current["batteries"][0]["capacity_kwh"] == 12
            assert current["batteries"][0]["soc_entity"] == "sensor.soc_new"
            assert current["version"] != original["version"]
            override = {**current, "expected_version": current["version"]}
            override["batteries"][0]["capacity_kwh"] = 10
            assert (
                await client.put("/v1/installation", json=override, headers=headers)
            ).status == 400
            setup["installation"] = override
            assert (
                await client.post("/v1/setup/preview", json=setup, headers=headers)
            ).status == 400

    asyncio.run(scenario())


@pytest.mark.parametrize("second_value,expected", [(40.3, 99.9), (39.9, None)])
def test_multi_export_bonus_sum_preserves_quota_and_detects_individual_reset(
    tmp_path, second_value, expected
):
    """99.6 used + .1 + .2 = 99.9 kWh. Physical resets do not affect annual sums but still block unsafe historical repricing."""
    from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
    from open_energy_planner.contracts.export_meter import export_identity, export_sources
    from open_energy_planner.home_assistant.observation_wire import parse_report
    from open_energy_planner.savings.repricing import initial_counters
    from tests.app.contract.test_contract_bonuses import rule
    from tests.app.contract.test_contract_prices import NOW, contract, input_source
    from tests.ha_contract.test_observation_inputs import report_body

    _metadata, settings = energy_installation()
    configuration = sources(parse(settings))
    body = report_body(configuration, now=NOW)
    for reading in body["readings"]:
        if reading["entity_id"] in ("sensor.export", "sensor.export2"):
            reading.update(
                unit="kWh", value=10.1 if reading["entity_id"] == "sensor.export" else second_value
            )
    report = parse_report(body, configuration, NOW)
    identity = export_identity(spec.entity_id for spec in export_sources(configuration))
    seed = CounterSeed(
        "solar",
        rule().period_start(NOW),
        99.6,
        50.1,
        identity,
        NOW - timedelta(minutes=1),
        (("sensor.export", 10), ("sensor.export2", 40.1)),
    )
    book = CounterBook(NOW, (seed,))
    source = input_source(tmp_path, rules=replace(contract(), bonuses=(rule(),)))
    from open_energy_planner.contracts.bonus_statistics import StatisticReference, StatisticsBasis

    endpoint = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    basis = StatisticsBasis(
        endpoint - timedelta(hours=1),
        endpoint,
        (
            StatisticReference("sensor.export", 1, "recorder", "kWh", 10, 10.1),
            StatisticReference("sensor.export2", 2, "recorder", "kWh", 40.1, 40.3),
        ),
        99.6,
        NOW,
        "a" * 64,
        True,
    )
    stats_seed = replace(
        seed,
        observed_at=endpoint,
        used_kwh=99.9,
        meter_kwh=50.4,
        component_meters=(("sensor.export", 10.1), ("sensor.export2", 40.3)),
        statistics=basis,
    )
    source.counters = lambda: CounterBook(NOW, (stats_seed,))
    plan = {
        "input_snapshot": {
            "problem": {"intervals": [{"start": NOW.isoformat()}]},
            "measurement_evidence": [
                {
                    "key": reading.key,
                    "entity_id": reading.entity_id,
                    "value": reading.value,
                    "observed_at": reading.observed_at.isoformat(),
                }
                for reading in report.readings
            ],
        }
    }
    rules = replace(contract(), bonuses=(rule(),))
    if expected is None:
        assert source.current(configuration, NOW, report=report).problem.export_bonuses[
            0
        ].initial_kwh == pytest.approx(99.9)
        with pytest.raises(ValueError, match="component"):
            initial_counters(plan, rules, book, None)
    else:
        result = source.current(configuration, NOW, report=report)
        assert result.problem.export_bonuses[0].initial_kwh == pytest.approx(expected)
        actual, reference = initial_counters(plan, rules, book, None)
        assert list(actual.values()) == pytest.approx([expected])
        assert reference == actual


def test_multi_export_history_seed_preserves_components_and_database_roundtrip(tmp_path):
    """Two meters add 2+3 kWh over four hours. Retain each endpoint so later resets remain detectable after restart."""
    from open_energy_planner.contracts.bonus_history import BonusHistory
    from open_energy_planner.contracts.export_meter import export_identity, export_sources
    from open_energy_planner.history.model import EnergyHistory, EnergyHour
    from open_energy_planner.storage.counters import CounterStore
    from tests.app.history.support.bonus import NOW, START, inputs
    from tests.database.support.archive import make_store

    contract, _configuration = inputs()
    _metadata, settings = energy_installation()
    configuration = sources(parse(settings))
    service = BonusHistory()
    request = service.request(contract, configuration, None, NOW)
    assert request.document()["entity_ids"] == ["sensor.export", "sensor.export2"]
    history = EnergyHistory(
        export_identity(spec.entity_id for spec in export_sources(configuration)),
        tuple(
            EnergyHour(START + timedelta(hours=index - 1), 100 + index * 1.25, 50 + index * 1.25)
            for index in range(5)
        ),
        (("sensor.export", 22), ("sensor.export2", 33)),
    )
    book = service.derive(request, history, None, NOW)
    assert book.seeds[0].used_kwh == 5
    assert book.seeds[0].meter_kwh == 55
    store = make_store(tmp_path, CounterStore)
    store.save(book)
    restored = make_store(tmp_path, CounterStore)
    assert restored.current == book
    assert service.request(contract, configuration, restored.current, NOW) is not None
