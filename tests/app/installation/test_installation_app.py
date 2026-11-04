"""Production live bootstrap over authenticated HTTP, with no synthetic installation/basis injection."""

import asyncio
import logging
from contextlib import asynccontextmanager, closing
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web
from sqlalchemy.exc import SQLAlchemyError

from custom_components.open_energy_planner.protocol import Plan as WirePlan
from open_energy_planner.app import create_app
from open_energy_planner.installation import api as installation_api
from open_energy_planner.installation.model import (
    BatteryState,
    InstallationProblem,
    InstalledBattery,
)
from open_energy_planner.installation.storage import parse as parse_installation
from open_energy_planner.optimization.domain import Interval
from open_energy_planner.planning import InputSnapshot
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.service import PriceService
from open_energy_planner.prices.storage.adapters.store import PriceStore
from open_energy_planner.savings.model import PendingSettlement
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.contract.support.settings import settings
from tests.app.demand.test_demand_app import definition
from tests.app.planning.test_live_app import TOKEN, Solver
from tests.app.solar.test_solar import SITE
from tests.app.solar.test_solar_app import fixture_weather
from tests.database.support.archive import list_documents, make_store, read_plan


def installation(count=0):
    """Declare optional independent 2 kWh batteries. No current SoC is fabricated as persistent configuration."""
    return {
        "grid_power_entity": "sensor.grid",
        "import_limit_w": 5000,
        "export_limit_w": 5000,
        "inverters": [
            {"inverter_id": f"inv{index}", "ac_limit_w": 2000, "efficiency": 0.96}
            for index in range(count)
        ],
        "batteries": [
            asdict(
                InstalledBattery(
                    f"bat{index}",
                    f"inv{index}",
                    f"sensor.soc{index}",
                    f"sensor.power{index}",
                    2,
                    1000,
                    1000,
                    energy_entity=f"sensor.discharge{index}",
                )
            )
            for index in range(count)
        ],
    }


async def observe(session, url, configuration, *, unavailable=None):
    """Forward normalized sensor readings for exactly the selected generation, or one explicit source fault."""
    now = datetime.now(UTC).isoformat()
    readings, issues = [], []
    for binding in configuration["bindings"]:
        if binding["key"] == unavailable:
            issues.append(
                {"key": binding["key"], "entity_id": binding["entity_id"], "reason": "unavailable"}
            )
        else:
            readings.append(
                {
                    "key": binding["key"],
                    "entity_id": binding["entity_id"],
                    "value": 50 if binding["quantity"] == "soc" else 0,
                    "unit": "%" if binding["quantity"] == "soc" else "W",
                    "observed_at": now,
                    "reported_at": now,
                    "timestamp_kind": "ha_state",
                }
            )
    body = {
        "version": now,
        "config_revision": configuration["revision"],
        "captured_at": now,
        "readings": readings,
        "issues": issues,
        "location": asdict(SITE),
    }
    async with session.post(url + "/v1/observations", json=body) as response:
        assert response.status == 200, await response.text()
        return await response.json()


@pytest.mark.parametrize(
    "count,solar,reference_error",
    [
        (0, False, None),
        (2, False, None),
        (1, True, None),
        pytest.param(0, False, FileNotFoundError, id="reference-document-missing"),
        pytest.param(0, False, OSError, id="reference-document-io-failure"),
        pytest.param(0, False, SQLAlchemyError, id="reference-database-failure"),
        pytest.param(0, False, ValueError, id="reference-value-failure"),
        pytest.param(0, False, KeyError, id="reference-key-failure"),
        pytest.param(0, False, TypeError, id="reference-type-failure"),
        pytest.param(0, False, PendingSettlement, id="reference-awaits-evidence"),
    ],
)
def test_live_bootstrap_builds_complete_plan_from_settings_forecasts_prices_and_measurements(
    tmp_path, count, solar, reference_error, monkeypatch, caplog
):
    """Live settings produce plans despite optional reference faults. Energy charge links create a retained second battery configuration."""
    if reference_error:
        from open_energy_planner.savings import forecast

        original_comparison = forecast.comparison

        def fail_reference(*arguments, **keywords):
            """Emulate a failed reference read without breaking the independently healthy plan archive."""
            if keywords.get("without_battery"):
                return original_comparison(*arguments, **keywords)
            raise reference_error(
                "Reference forecast context is missing"
                if reference_error is PendingSettlement
                else "postgresql://private:SECRET_PASSWORD@private.invalid/archive?token=SECRET_TOKEN"
            )

        monkeypatch.setattr(forecast, "comparison", fail_reference)

    async def scenario():
        """Use real HTTP, physical forecasting and HiGHS. Only external weather and market responses are fixtures."""
        now = datetime.now(UTC)
        from open_energy_planner.storage.adapters.documents import SqlDocuments

        stale_reference = {
            "end": (now - timedelta(days=3)).isoformat(),
            "reference_stored": {f"bat{index}": 0 for index in range(count)},
            "actual_stored": {f"bat{index}": 1 for index in range(count)},
            "capacities": {f"bat{index}": 2 for index in range(count)},
        }
        with closing(SqlPlanRepository(tmp_path)) as archive:
            SqlDocuments(archive.engine, "savings").write(
                "physical-reference.json", stale_reference
            )
        start = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
        price = PriceSeries(
            "fixture",
            "NL",
            "EUR",
            now,
            now,
            None,
            tuple(
                PricePoint(
                    start + timedelta(minutes=15 * index),
                    start + timedelta(minutes=15 * (index + 1)),
                    0.2,
                    PriceKind.PUBLISHED,
                    15,
                )
                for index in range(192)
            ),
        )
        store = make_store(tmp_path, PriceStore)
        store.save((price,))

        @asynccontextmanager
        async def prices(_database_engine):
            """Yield a normalized cached market response without enabling external provider HTTP."""
            yield PriceService("NL", None, None, store)

        runner = web.AppRunner(
            create_app(
                tmp_path,
                TOKEN,
                source="home_assistant",
                solver=Solver(),
                price_factory=prices,
                solar_weather_factory=fixture_weather,
            )
        )
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
                for path, body in (("contracts", settings()), ("demand", definition())):
                    if path == "demand" and count == 0:
                        body["terms"] = []
                    async with session.put(url + "/v1/" + path, json=body) as response:
                        assert response.status == 200, await response.text()
                solar_definition = {"sources": [], "inverters": []}
                physical = installation(count)
                if count:
                    physical["batteries"][0].update(minimum_soc=60, recover_reserve=True)
                if solar:
                    solar_definition = {
                        "sources": [
                            {
                                "source_id": "roof",
                                "entity_id": "sensor.roof",
                                "inverter_id": "inv0",
                                "capacity_w": 1000,
                                "orientation": "south",
                                "can_curtail": True,
                            },
                            {
                                "source_id": "shed",
                                "entity_id": "sensor.shed",
                                "inverter_id": "shed_inv",
                                "capacity_w": 500,
                                "orientation": "west",
                            },
                        ],
                        "inverters": [
                            {
                                "inverter_id": "inv0",
                                "ac_limit_w": 2000,
                                "efficiency": 0.96,
                                "hybrid": True,
                            },
                            {"inverter_id": "shed_inv", "ac_limit_w": 500, "efficiency": 0.95},
                        ],
                    }
                    physical["inverters"][0]["hybrid"] = True
                async with session.put(url + "/v1/solar", json=solar_definition) as response:
                    assert response.status == 200, await response.text()
                async with session.put(url + "/v1/installation", json=physical) as response:
                    assert response.status == 200, await response.text()
                    state = await response.json()
                from tests.ha_contract.support.metadata import setup_metadata

                metadata = setup_metadata()
                terms = [{"entity_id": "sensor.house", "sign": 1}]
                terms.extend(
                    {"entity_id": source["entity_id"], "sign": 1}
                    for source in solar_definition["sources"]
                )
                for battery in physical["batteries"]:
                    terms.extend(
                        [
                            {"entity_id": battery["energy_entity"], "sign": 1},
                            {"entity_id": "sensor.charge_" + battery["device_id"], "sign": -1},
                        ]
                    )
                if count or solar:
                    terms.append({"entity_id": "sensor.export", "sign": -1})
                async with session.put(
                    url + "/v1/demand",
                    json=definition(
                        terms=terms, expected_version=state["demand"]["configuration"]["version"]
                    ),
                ) as response:
                    assert response.status == 200, await response.text()
                metadata["suggestions"].update(
                    terms=terms,
                    solar=bool(solar),
                    solar_sources=[
                        source_item["entity_id"] for source_item in solar_definition["sources"]
                    ],
                    batteries=[
                        {
                            "energy_entity": battery_item["energy_entity"],
                            "soc_entity": battery_item["soc_entity"],
                            "power_entity": battery_item["power_entity"],
                            "capacity_kwh": battery_item["capacity_kwh"],
                            "charge_energy_entity": "sensor.charge_" + battery_item["device_id"],
                        }
                        for battery_item in physical["batteries"]
                    ],
                )
                async with session.post(url + "/v1/ha/catalogue", json=metadata) as response:
                    assert response.status == 200, await response.text()
                previous_revision = state["measurement_config"]["revision"]
                async with session.get(url + "/v1/snapshot") as response:
                    state = await response.json()
                config = state["measurement_config"]
                assert (config["revision"] != previous_revision) is bool(count)
                assert len(config["bindings"]) == 1 + 2 * count
                await observe(session, url, config)
                for _ in range(500):
                    async with session.get(url + "/v1/snapshot") as response:
                        state = await response.json()
                    if state["plan"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError(state["planning"])
                plan = state["plan"]
                WirePlan.parse(plan, config["revision"])
                assert plan["device_commands"] is True
                if not reference_error:
                    archived = read_plan(tmp_path, plan["plan_id"])
                    reference = archived["reference_forecast"]
                    assert reference["reference_start"] == {
                        "stored": None,
                        "counters": {},
                        "status": "measured_common_start",
                        "estimated": False,
                    }
                    assert "reference_forecast_issue" not in archived
                    with closing(SqlPlanRepository(tmp_path)) as archive:
                        assert (
                            SqlDocuments(archive.engine, "savings").read("physical-reference.json")
                            == stale_reference
                        )
                if reference_error:
                    archived = read_plan(tmp_path, plan["plan_id"])
                    expected_issue = (
                        "Reference forecast context is missing"
                        if reference_error is PendingSettlement
                        else "Reference forecast is unavailable. Plan publication continues. Inspect application diagnostics."
                    )
                    assert archived["reference_forecast_issue"] == expected_issue
                    assert "reference_forecast" not in archived
                    async with session.get(
                        url + "/v1/plans/" + plan["plan_id"] + "/comparison"
                    ) as response:
                        assert response.status == 200
                        comparison = await response.json()
                        assert comparison["comparison_issue"] == expected_issue
                        assert comparison["plan"]["reference_forecast_issue"] == expected_issue
                    async with session.get(url + "/v1/plans/" + plan["plan_id"]) as response:
                        assert response.status == 200
                        assert (await response.json())["reference_forecast_issue"] == expected_issue
                    if reference_error is not PendingSettlement:
                        assert any(
                            record.name == "open_energy_planner.app"
                            and record.levelno == logging.ERROR
                            and reference_error.__name__ in record.message
                            and "fail_reference" in record.message
                            for record in caplog.records
                        )
                    assert "SECRET_PASSWORD" not in caplog.text
                    assert "SECRET_TOKEN" not in caplog.text
                    assert "private.invalid" not in caplog.text
                    if reference_error is ValueError:
                        retained = {
                            **archived,
                            "plan_id": plan["plan_id"] + "-retained",
                            "reference_forecast_issue": (
                                "postgresql://private:SECRET_PASSWORD@private.invalid/archive?token=SECRET_TOKEN"
                            ),
                        }
                        with closing(SqlPlanRepository(tmp_path)) as archive:
                            archive.state["plan"] = retained
                            archive.save()
                            for suffix in ("", "/comparison"):
                                async with session.get(
                                    url + "/v1/plans/" + retained["plan_id"] + suffix
                                ) as response:
                                    assert response.status == 200
                                    text = await response.text()
                                    assert "SECRET_PASSWORD" not in text
                                    assert "SECRET_TOKEN" not in text
                                    assert "private.invalid" not in text
                                    result = await response.json()
                                    if suffix:
                                        assert result["comparison_issue"] == expected_issue
                                        result = result["plan"]
                                    assert result["reference_forecast_issue"] == expected_issue
                            assert archive.get_plan(retained["plan_id"]) == retained
                assert len(plan["steps"]) >= 120
                assert len(plan["input_snapshot"]["problem"]["batteries"]) == count
                values = plan["input_snapshot"]["problem"]["terminal_valuations"]
                assert len(values) == count
                assert all(value["method"] == "bounded-average-v1" for value in values)
                final = {
                    battery_item["device_id"]: battery_item["end_kwh"]
                    for battery_item in plan["expected_flows"][-1]["batteries"]
                }
                inventory = sum(
                    min(
                        max(0, final[value["device_id"]] - value["reserve_kwh"]),
                        value["usable_kwh"],
                    )
                    * value["rate"]
                    for value in values
                )
                assert plan["independent_costs"]["terminal_value"] == pytest.approx(inventory)
                assert len(plan["steps"][0]["devices"]) == count + int(solar)
                assert plan["input_snapshot"]["demand_evidence"]
                assert bool(plan["input_snapshot"]["solar_evidence"]) == solar
                if count:
                    cell = plan["input_snapshot"]["problem"]["batteries"][0]["battery"]
                    assert cell["initial_kwh"] == 1
                    assert cell["minimum_kwh"] == 1.2
                    assert cell["recover_reserve"] is True
                    assert plan["expected_flows"][0]["batteries"][0]["charge_kwh"] > 0
                    assert (
                        next(
                            device
                            for device in state["installation"]["devices"]
                            if device["id"] == "bat0"
                        )["reserve_status"]
                        == "recovery_required"
                    )

                    class ProjectionClock(datetime):
                        """Read reserve projections at the validated publication instant, before input expiry."""

                        @classmethod
                        def now(cls, tz=None):
                            """Keep API reads in the same valid first interval as the accepted candidate."""
                            return datetime.fromisoformat(plan["generated_at"]).astimezone(tz)

                    with monkeypatch.context() as projection:
                        projection.setattr(installation_api, "datetime", ProjectionClock)
                        for path in ("setup", "installation"):
                            async with session.get(url + "/v1/" + path) as response:
                                devices = (await response.json())["devices"]
                                assert (
                                    next(device for device in devices if device["id"] == "bat0")[
                                        "reserve_status"
                                    ]
                                    == "recovery_required"
                                )
                    state = await observe(session, url, config, unavailable="battery.bat0.soc")
                    assert state["plan"] is None
                    assert state["planning"]["reason"] == "required_measurement_unavailable"
                async with session.put(
                    url + "/v1/installation",
                    json={**state["installation"]["configuration"], "expected_version": "stale"},
                ) as response:
                    assert response.status == 409
                async with session.put(
                    url + "/v1/sources",
                    json={"bindings": config["bindings"], "expected_revision": config["revision"]},
                ) as response:
                    assert response.status == 409
                definitions = list_documents(tmp_path, "installation")
                assert len(definitions) == (3 if count else 2)
                assert previous_revision + ".json" in definitions
                assert config["revision"] + ".json" in definitions
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("elapsed_ms", "expected_status"),
    [(0, "recovery_required"), (99, "recovery_required"), (100, "measurement_unavailable")],
)
def test_reserve_projection_rejects_an_ended_first_interval_with_fresh_measurements(
    monkeypatch, elapsed_ms, expected_status
):
    """Report recovery_required before 09:15 and measurement_unavailable at 09:15.

    The first interval ends despite a 0.1-second-old SoC reading. Keep 2 kWh
    capacity and the 60% reserve unchanged.
    """
    measured_at = datetime(2026, 10, 28, 9, 14, 59, 900000, tzinfo=UTC)
    physical = installation(1)
    physical["batteries"][0].update(minimum_soc=60, recover_reserve=True)
    settings = parse_installation(physical)
    battery_definition = settings.batteries[0]
    problem = InstallationProblem(
        (Interval(measured_at, 0.1 / 3600, 0.2, 0.1),),
        settings.inverters,
        (
            BatteryState(
                battery_definition.device_id,
                battery_definition.inverter_id,
                battery_definition.state(50),
            ),
        ),
        (),
        5000,
        5000,
    )
    snapshot = InputSnapshot(
        "reserve-boundary",
        settings.version,
        measured_at,
        "measurement",
        "forecast",
        "prices",
        problem,
        source="home_assistant",
        maximum_input_age_s=300,
    )
    instant = measured_at + timedelta(milliseconds=elapsed_ms)

    class ProjectionClock(datetime):
        """Select instants immediately before and exactly at the first-interval boundary."""

        @classmethod
        def now(cls, tz=None):
            """Return the selected UTC instant without changing measurement or planning policy."""
            return instant.astimezone(tz)

    monkeypatch.setattr(installation_api, "datetime", ProjectionClock)
    devices = installation_api.catalogue(
        SimpleNamespace(current=settings), SimpleNamespace(configuration=None), snapshot
    )
    assert devices[0]["reserve_status"] == expected_status
    assert devices[0]["configuration"]["capacity_kwh"] == 2
    assert devices[0]["configuration"]["minimum_soc"] == 60
    assert problem.batteries[0].battery.initial_kwh == 1
    assert problem.batteries[0].battery.minimum_kwh == 1.2
    assert 0 <= (instant - measured_at).total_seconds() < 300
    assert snapshot.current_at(instant) is (elapsed_ms < 100)
