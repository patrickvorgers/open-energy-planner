"""Thermal authority, durable settings and complete forecast admission."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from math import exp
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from open_energy_planner.appliances.cycles.service import ApplianceService
from open_energy_planner.appliances.ev.service import EVService
from open_energy_planner.appliances.forecasting import ConsumerForecasts, build_components
from open_energy_planner.appliances.thermal.service import ThermalService
from open_energy_planner.demand.calculation.model import DemandConfiguration, EnergyTerm
from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.installation.commands import plan_document
from open_energy_planner.installation.model import InstallationProblem
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.domain import Interval
from open_energy_planner.planning import InputSnapshot
from open_energy_planner.storage.adapters import documents as document_adapter
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from tests.database.support.backends import BACKENDS

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
DEVICE = "00000000-0000-4000-8000-000000000010"
ENERGY = "sensor.boiler_energy"


def catalogue():
    """Keep the boiler's electrical meter explicitly owned by Home Assistant Energy."""
    return {
        "entities": [
            {"entity_id": ENERGY, "device_class": "energy", "unit": "kWh", "device_name": "Boiler"},
            {
                "entity_id": "sensor.boiler_temperature",
                "device_class": "temperature",
                "unit": "°C",
            },
            {"entity_id": "sensor.boiler_power", "device_class": "power", "unit": "W"},
        ],
        "suggestions": {"consumers": [{"entity_id": ENERGY, "upstream": None}]},
    }


def settings():
    """Use declared hourly heat demand without inferring hot water from electrical input."""
    return {
        "stores": [
            {
                "id": DEVICE,
                "energy_entity": ENERGY,
                "temperature_entity": "sensor.boiler_temperature",
                "power_entity": "sensor.boiler_power",
                "volume_litres": 100,
                "minimum_c": 45,
                "maximum_c": 70,
                "ambient_c": 20,
                "loss_w_per_c": 2,
                "heater_w": 2000,
                "draw_profile_kwh": [0.2] * 24,
                "enabled": True,
            }
        ]
    }


@pytest.fixture(params=BACKENDS, ids=[name for name, _ in BACKENDS])
def service(request, monkeypatch):
    """Exercise real SQL document transactions and the existing appliance/EV preparation chain."""
    backend, url = request.param
    engine = create_engine("sqlite://" if backend == "sqlite" else url)
    SCHEMA.create_all(engine)
    namespace = "thermal-test-" + uuid4().hex
    monkeypatch.setattr(
        document_adapter,
        "DOCUMENT_NAMESPACES",
        (*document_adapter.DOCUMENT_NAMESPACES, namespace, namespace + "-demand"),
    )
    documents = SqlDocuments(engine, namespace)
    consumers = ConsumerForecasts(SqlDocuments(engine, namespace + "-demand"))
    consumers.sync(catalogue())
    appliances = ApplianceService(documents, consumers)
    ev = EVService(documents, consumers, appliances)
    app = ThermalService(documents, consumers, appliances, ev)
    app.configure(settings(), catalogue())
    yield app
    engine.dispose()


def report(service, *, observed=NOW, temperature=55, power=0):
    """Report explicit actuals. Zero electrical power does not imply a zero tank temperature."""
    return {
        "configuration_version": service.configuration_version,
        "at": NOW.isoformat(),
        "readings": [
            {
                "id": DEVICE,
                "temperature_c": temperature,
                "power_w": power,
                "observed_at": observed.isoformat(),
            }
        ],
    }


def snapshot():
    """Total household demand is 1 kWh, of which the boiler baseline contributes 0.4 kWh."""
    inputs = InstallationProblem((Interval(NOW, 1, 0.3, 0, load_kwh=1),), (), (), (), 5000, 5000)
    return InputSnapshot("snapshot", "config", NOW, "measurement", "forecast", "prices", inputs)


def forecasts(service):
    """Archive independent residual and boiler component forecasts over the whole hour."""

    def forecast(energy):
        """Represent an issued prediction without any knowledge of later measurements."""
        return {
            "issued_at": NOW.isoformat(),
            "steps": [
                {"start": NOW.isoformat(), "duration_h": 1, "kwh": energy},
            ],
        }

    service.consumers.result = {
        "residual_baseline": forecast(0.6),
        "devices": [
            {"entity_id": ENERGY, "forecast": forecast(0.4), "baseline_forecast": forecast(0.4)}
        ],
    }


def prepare(service, now=NOW):
    """Use production preparation and real consumer evidence, without fabricating a residual result."""
    return service.prepare(snapshot(), SimpleNamespace(), now, "Europe/Amsterdam")


def test_real_mixed_preparation_preserves_cycle_ev_thermal_ownership_through_publication(service):
    """The real thermal-to-EV-to-cycle chain separates .7 kWh once and publishes all three owned loads."""
    from open_energy_planner.optimization.adapters.highs import HighsSolver
    from tests.app.ev.support.devices import CAR, POINT, SESSION, observations
    from tests.app.ev.support.devices import catalogue as ev_catalogue
    from tests.app.ev.support.devices import settings as ev_settings

    cycle_entity = "sensor.cycle_energy"
    discovery = catalogue()
    charging = ev_catalogue()
    discovery["entities"].extend(charging["entities"])
    discovery["entities"].append({"entity_id": cycle_entity})
    discovery["suggestions"]["consumers"].extend(charging["suggestions"]["consumers"])
    discovery["suggestions"]["consumers"].append({"entity_id": cycle_entity, "upstream": None})
    service.consumers.sync(discovery)
    charging_settings = ev_settings()
    charging_settings["vehicles"][0].update(target_soc=51, departure_time="14:00")
    service.ev.configure(charging_settings, discovery)
    service.ev.receive(observations(service.ev), NOW)
    service.ev.assign(POINT, CAR, SESSION, NOW)
    service.consumers.save(
        (*service.consumers.selected, {"entity_id": cycle_entity, "weather_sensitive": False}),
        service.consumers.version,
    )
    service.appliances.configure(cycle_entity, True)
    service.appliances.set_manual_profile(cycle_entity, 15, 0.25, service.appliances.revision)
    service.appliances.submit(cycle_entity, "ready-cycle", NOW, NOW + timedelta(hours=1), NOW)
    forecasts(service)
    component_result = service.consumers.result
    component_result["residual_baseline"]["steps"][0]["kwh"] = 0.3
    for entity, energy in (("sensor.charger_energy", 0.2), (cycle_entity, 0.1)):
        forecast = deepcopy(component_result["residual_baseline"])
        forecast["steps"][0]["kwh"] = energy
        component_result["devices"].append(
            {"entity_id": entity, "forecast": deepcopy(forecast), "baseline_forecast": forecast}
        )
    service.receive(report(service), NOW)
    prepared = prepare(service)
    assert (
        len(prepared.problem.appliances)
        == len(prepared.problem.ev_work)
        == len(prepared.problem.thermal_work)
        == 1
    )
    assert {component.entity_id for component in prepared.load_components} == {
        None,
        ENERGY,
        cycle_entity,
        "sensor.charger_energy",
    }
    assert sum(interval.load_kwh for interval in prepared.problem.intervals) == pytest.approx(
        0.3 + 0.2 * 130 / 3600
    )
    solved = HighsSolver().solve(prepared.problem)
    assert solved.plan is not None, solved.stop_reason
    costs = validate(prepared.problem, solved.plan)
    published = plan_document(prepared, solved, costs, NOW)
    assert len(published["appliance_schedule"]) == 1
    assert len(published["ev_context"]["work"]) == 1
    assert len(published["thermal_context"]["devices"]) == 1


def test_configuration_is_immutable_and_survives_restart_without_restoring_current_actuals(service):
    """Caller mutation cannot alter durable settings. Restart restores settings but requires fresh telemetry."""
    candidate = settings()
    service.configure(candidate, catalogue())
    version = service.configuration_version
    candidate["stores"][0]["maximum_c"] = 90
    service.receive(report(service), NOW)
    assert service.state["stores"][0]["maximum_c"] == 70
    restored = ThermalService(service.documents, service.consumers, service.appliances, service.ev)
    assert restored.configuration_version == version
    assert restored.document(NOW)["devices"][0]["actual"] is None
    assert restored.eligible(NOW) == []


def test_configuration_rejects_non_energy_meter_without_changing_saved_state(service):
    """A normal sensor outside the Energy catalogue cannot become a controlled heat store."""
    candidate = settings()
    candidate["stores"][0]["energy_entity"] = "sensor.unregistered"
    version = service.configuration_version
    with pytest.raises(ValueError):
        service.configure(candidate, catalogue())
    assert service.configuration_version == version


@pytest.mark.parametrize(
    "field,value",
    [("temperature_c", None), ("power_w", None), ("temperature_c", True), ("power_w", -1)],
)
def test_missing_or_invalid_actuals_never_become_zero(service, field, value):
    """Unavailable physical evidence cannot authorize replacement of the household baseline."""
    body = report(service)
    body["readings"][0][field] = value
    with pytest.raises(ValueError):
        service.receive(body, NOW)
    assert service.document(NOW)["devices"][0]["actual"] is None
    assert prepare(service).problem.thermal_work == ()


def test_stale_and_out_of_order_reports_do_not_replace_newer_temperature(service):
    """An earlier valid report and an expired report must not overwrite the 55-C current reading."""
    service.receive(report(service), NOW)
    for captured in (NOW - timedelta(seconds=1), NOW - timedelta(minutes=6)):
        body = report(service, observed=captured, temperature=10)
        body["at"] = captured.isoformat()
        with pytest.raises(ValueError):
            service.receive(body, NOW)
    assert service.document(NOW)["devices"][0]["actual"]["temperature_c"] == 55
    assert service.document(NOW + timedelta(minutes=5))["devices"][0]["actual"] is None


def test_unchanged_thermal_values_keep_provenance_but_capture_expiry_still_applies(service):
    """A stable tank at 55 C and zero watts remains measurable without an invented recent sensor timestamp."""
    observed = NOW - timedelta(hours=8)
    service.receive(report(service, observed=observed), NOW)
    actual = service.document(NOW)["devices"][0]["actual"]
    assert actual["temperature_c"] == 55
    assert actual["power_w"] == 0
    assert service.readings[DEVICE]["observed_at"] == observed.isoformat()
    assert service.document(NOW + timedelta(minutes=5))["devices"][0]["actual"] is None


def test_old_sensor_timestamp_does_not_override_thermal_capture_order(service):
    """Newer complete captures accept old sensor metadata. An empty newer capture cannot be undone by an older report."""
    service.receive(report(service), NOW)
    later = NOW + timedelta(seconds=5)
    body = report(service, observed=NOW - timedelta(hours=8), temperature=54)
    body["at"] = later.isoformat()
    service.receive(body, later)
    assert service.document(later)["devices"][0]["actual"]["temperature_c"] == 54
    empty = {**body, "at": (later + timedelta(seconds=5)).isoformat(), "readings": []}
    service.receive(empty, later + timedelta(seconds=5))
    with pytest.raises(ValueError, match="capture went backwards"):
        service.receive(body, later + timedelta(seconds=5))
    assert service.readings == {}


def test_complete_components_remove_boiler_baseline_once_and_freeze_provenance(service):
    """Replace the original 1-kWh household boundary with its 0.6-kWh residual, plus modeled heating."""
    forecasts(service)
    service.receive(report(service), NOW)
    prepared = prepare(service)
    assert sum(step.load_kwh for step in prepared.problem.intervals) == pytest.approx(0.6)
    assert len(prepared.problem.thermal_work) == 1
    assert prepared.problem.thermal_work[0].terminal_c == 55
    assert sum(prepared.problem.thermal_work[0].demand_kwh) == pytest.approx(0.2)
    evidence = deepcopy(prepared.thermal_evidence)
    service.receive(report(service, temperature=50), NOW)
    assert prepared.thermal_evidence == evidence
    assert prepared.thermal_evidence[0]["demand_source"] == "configured_heat_draw_profile"
    assert prepared.future_valid_until == snapshot().future_valid_until
    repeated = prepare(service)
    assert sum(step.load_kwh for step in repeated.problem.intervals) == pytest.approx(0.6)


def test_unrelated_short_history_keeps_ready_boiler_and_unmodelled_household_load(service):
    """A 59-hour dryer stays in the 0.6-kWh residual while the measured 0.4-kWh boiler is replaced once."""
    metadata = catalogue()
    metadata["entities"].append({"entity_id": "sensor.dryer", "device_name": "New dryer"})
    metadata["suggestions"]["consumers"].append({"entity_id": "sensor.dryer", "upstream": None})
    consumers = service.consumers
    consumers.sync(metadata)
    consumers.save(
        (*consumers.selected, {"entity_id": "sensor.dryer", "weather_sensitive": False}),
        consumers.version,
    )

    def history(entity, hourly_kwh, hours):
        """Build independent cumulative observations ending one hour before capture."""
        return EnergyHistory(
            entity,
            tuple(
                EnergyHour(
                    NOW - timedelta(hours=hours + 1 - index),
                    100 + index * hourly_kwh,
                    100 + index * hourly_kwh,
                )
                for index in range(hours + 1)
            ),
        )

    consumers.histories = {
        ENERGY: history(ENERGY, 0.4, 35 * 24),
        "sensor.dryer": history("sensor.dryer", 0.1, 59),
    }
    candidate, result = build_components(
        DemandConfiguration("Europe/Amsterdam", 8760, (EnergyTerm("sensor.house", 1),)),
        (history("sensor.house", 1, 35 * 24),),
        (),
        NOW,
        consumers.inputs(),
    )
    assert candidate.model == "consumer-composite-v1"
    assert result["status"] == "partial_history"
    assert [row["entity_id"] for row in result["devices"]] == [ENERGY]
    consumers.result = result
    service.receive(report(service), NOW)
    prepared = prepare(service)
    assert len(prepared.problem.thermal_work) == 1
    assert sum(step.load_kwh for step in prepared.problem.intervals) == pytest.approx(0.6)
    assert snapshot().problem.intervals[0].load_kwh == 1


def test_dispatch_lead_is_not_shortened_by_minute_rounding(service):
    """A capture at :59 retains the full solver budget plus a publication minute before heating."""
    now = NOW + timedelta(seconds=59)
    forecasts(service)
    body = report(service, observed=now)
    body["at"] = now.isoformat()
    service.receive(body, now)
    prepared = prepare(service, now)
    work = prepared.problem.thermal_work[0]
    assert work.ready_at == NOW + timedelta(minutes=4)
    assert (work.ready_at - now).total_seconds() == 181
    assert work.state_at == now
    assert sum(work.demand_kwh) == pytest.approx(0.2 * (3600 - 59) / 3600)


@pytest.mark.parametrize("power", [0, 2000])
def test_dispatch_lead_preserves_passive_physics_and_continuous_measured_heating(service, power):
    """New heat waits three minutes. Existing measured heat continues, while draw and losses never pause."""
    from open_energy_planner.optimization.adapters.highs import HighsSolver

    configuration = settings()
    configuration["stores"][0]["minimum_c"] = 60
    service.configure(configuration, catalogue())
    forecasts(service)
    service.receive(report(service, power=power), NOW)
    prepared = prepare(service)
    work = prepared.problem.thermal_work[0]
    assert work.initially_heating is (power == 2000)
    assert work.ready_at == (NOW if power else NOW + timedelta(minutes=3))
    result = HighsSolver().solve(prepared.problem)
    assert result.plan is not None, result.stop_reason
    validate(prepared.problem, result.plan)
    first = result.plan.flows[0].thermal[0]
    capacity = 100 * 4.186 / 3600
    exponent = 0.002 * 0.05 / capacity
    decay = exp(-exponent)
    gain = (1 - decay) / exponent
    expected = 20 + 35 * decay + (power / 1000 * 0.05 - 0.01) * gain / capacity
    assert first.power_w == power
    assert first.end_c == pytest.approx(expected)


@pytest.mark.parametrize(
    "defect",
    [
        "absent",
        "expired_residual",
        "incomplete_residual",
        "incomplete_component",
        "expired_component",
        "incomplete_component_baseline",
        "expired_component_baseline",
        "component_gap",
        "future_component",
    ],
)
def test_incomplete_forecast_retains_household_boundary_and_withdraws_heat_work(service, defect):
    """A meter identity alone cannot prove a complete component prediction for the planning horizon."""
    forecasts(service)
    service.receive(report(service), NOW)
    if defect == "absent":
        service.consumers.result["devices"] = []
    elif defect == "expired_residual":
        service.consumers.result["residual_baseline"]["issued_at"] = (
            NOW - timedelta(hours=1)
        ).isoformat()
    elif defect == "incomplete_residual":
        service.consumers.result["residual_baseline"]["steps"] = []
    elif defect == "incomplete_component":
        service.consumers.result["devices"][0]["forecast"]["steps"] = []
    elif defect == "expired_component":
        service.consumers.result["devices"][0]["forecast"]["issued_at"] = (
            NOW - timedelta(hours=1)
        ).isoformat()
    elif defect == "incomplete_component_baseline":
        service.consumers.result["devices"][0]["baseline_forecast"]["steps"] = []
    elif defect == "expired_component_baseline":
        service.consumers.result["devices"][0]["baseline_forecast"]["issued_at"] = (
            NOW - timedelta(hours=1)
        ).isoformat()
    elif defect == "component_gap":
        service.consumers.result["devices"][0]["forecast"]["steps"][0]["duration_h"] = 0.75
    else:
        service.consumers.result["devices"][0]["forecast"]["issued_at"] = (
            NOW + timedelta(minutes=1)
        ).isoformat()
    prepared = prepare(service)
    assert prepared.problem.thermal_work == ()
    assert sum(step.load_kwh for step in prepared.problem.intervals) == pytest.approx(1)
    assert prepared.thermal_evidence == ()
    assert service.ev.issue is None


def test_enabled_store_requires_declared_heat_demand_instead_of_inventing_draw(service):
    """Electrical consumer history cannot distinguish heat loss from hot-water extraction."""
    candidate = settings()
    candidate["stores"][0]["draw_profile_kwh"] = [0] * 24
    with pytest.raises(ValueError):
        service.configure(candidate, catalogue())


@pytest.mark.parametrize("boiler_forecast", ["absent", "incomplete"])
def test_unavailable_boiler_forecast_preserves_independently_ready_ev(service, boiler_forecast):
    """A failed boiler exclusion retains its 0.4 kWh baseline while removing only the proven 0.2 kWh charger baseline."""
    from tests.app.ev.support.devices import CAR, POINT, SESSION, observations
    from tests.app.ev.support.devices import catalogue as ev_catalogue
    from tests.app.ev.support.devices import settings as ev_settings

    discovery = catalogue()
    charging = ev_catalogue()
    discovery["entities"].extend(charging["entities"])
    discovery["suggestions"]["consumers"].extend(charging["suggestions"]["consumers"])
    service.consumers.sync(discovery)
    service.ev.configure(ev_settings(), discovery)
    service.ev.receive(observations(service.ev), NOW)
    service.ev.assign(POINT, CAR, SESSION, NOW)
    forecasts(service)
    result = service.consumers.result
    charger_forecast = deepcopy(result["residual_baseline"])
    charger_forecast["steps"][0]["kwh"] = 0.2
    result["devices"].append(
        {
            "entity_id": "sensor.charger_energy",
            "forecast": deepcopy(charger_forecast),
            "baseline_forecast": charger_forecast,
        }
    )
    if boiler_forecast == "absent":
        result["devices"].pop(0)
        result["residual_baseline"]["steps"][0]["kwh"] = 0.8
    else:
        result["residual_baseline"]["steps"][0]["kwh"] = 0.4
        result["devices"][0]["forecast"]["steps"] = []
    service.receive(report(service), NOW)

    prepared = prepare(service)

    assert prepared.problem.thermal_work == ()
    assert len(prepared.problem.ev_work) == 1
    assert prepared.problem.ev_work[0].point_id == POINT
    assert prepared.problem.ev_work[0].vehicle_id == CAR
    ready = prepared.problem.ev_work[0].ready_at
    # The .2-kWh/h charger expectation remains before its admitted control window.
    expected = 0.8 + 0.2 * (ready - NOW).total_seconds() / 3600
    assert sum(step.load_kwh for step in prepared.problem.intervals) == pytest.approx(expected)
    assert prepared.ev_evidence and prepared.appliance_evidence
    assert prepared.thermal_evidence == ()
    assert service.ev.issue is None
    assert service.issue == "Thermal component forecast unavailable. Retaining household baseline"


@pytest.mark.parametrize("ambient,maximum", [(-1, 70), (55, 50)])
def test_unsupported_passive_temperature_cannot_break_the_shared_model(service, ambient, maximum):
    """Freezing or surroundings hotter than the ceiling cannot be represented by this non-freezing tank model."""
    candidate = settings()
    candidate["stores"][0].update(ambient_c=ambient, maximum_c=maximum)
    saved = deepcopy(service.state)
    with pytest.raises(ValueError):
        service.configure(candidate, catalogue())
    assert service.state == saved


def test_expired_observations_change_revision_and_remove_previously_admitted_heat_work(service):
    """A previously planned store loses command authority after five minutes without telemetry."""
    forecasts(service)
    service.receive(report(service), NOW)
    admitted = prepare(service)
    assert admitted.problem.thermal_work
    expired = prepare(service, NOW + timedelta(minutes=5))
    assert expired.problem.thermal_work == ()
    assert expired.thermal_revision != admitted.thermal_revision
    assert sum(step.load_kwh for step in expired.problem.intervals) == pytest.approx(1)


def test_water_heater_temperature_binding_does_not_require_an_invented_thermostat_status(service):
    """A normal HA water_heater temperature and separate electrical power suffice for observation."""
    candidate = settings()
    candidate["stores"][0]["temperature_entity"] = "water_heater.boiler"
    discovery = catalogue()
    discovery["entities"][1]["entity_id"] = "water_heater.boiler"
    service.configure(candidate, discovery)
    service.receive(report(service, temperature=55, power=0), NOW)
    forecasts(service)
    prepared = prepare(service)
    assert prepared.problem.thermal_work[0].initial_c == 55
    assert prepared.thermal_evidence[0]["reading"]["power_w"] == 0
    assert service.document(NOW, discovery)["devices"][0]["name"] == "Boiler"


def test_removed_energy_membership_withdraws_heat_control_without_discarding_settings(service):
    """Changing HA Energy ownership removes permission, while keeping the user's stored configuration."""
    forecasts(service)
    service.receive(report(service), NOW)
    saved = deepcopy(service.state)
    assert prepare(service).problem.thermal_work
    discovery = catalogue()
    discovery["suggestions"]["consumers"] = []
    service.consumers.sync(discovery)
    assert prepare(service).problem.thermal_work == ()
    assert service.state == saved
    assert "Home Assistant Energy" in service.document(NOW)["devices"][0]["issue"]


def test_over_temperature_observation_withdraws_control_instead_of_clamping_actuals(service):
    """Measured 75 C above the configured 70-C ceiling remains 75 C and authorizes no heater plan."""
    forecasts(service)
    service.receive(report(service, temperature=75), NOW)
    assert service.document(NOW)["devices"][0]["actual"]["temperature_c"] == 75
    assert service.document(NOW)["devices"][0]["issue"]
    prepared = prepare(service)
    assert prepared.problem.thermal_work == ()
    assert sum(step.load_kwh for step in prepared.problem.intervals) == pytest.approx(1)


@pytest.mark.parametrize("owner", ["ev", "appliance"])
def test_later_control_ownership_conflict_withdraws_thermal_work_without_losing_settings(
    service, owner
):
    """A meter claimed after boiler setup cannot power two independently planned loads."""
    forecasts(service)
    service.receive(report(service), NOW)
    assert prepare(service).problem.thermal_work
    saved = deepcopy(service.state)
    if owner == "ev":
        service.ev.state["points"].append({"id": "point", "energy_entity": ENERGY})
    else:
        service.appliances.state["devices"][ENERGY] = {"shiftable": True}
    withdrawn = prepare(service)
    assert withdrawn.problem.thermal_work == ()
    assert service.document(NOW)["devices"][0]["issue"]
    assert sum(step.load_kwh for step in withdrawn.problem.intervals) == pytest.approx(1)
    assert service.state == saved


def test_unreadable_saved_configuration_cannot_be_overwritten_by_a_new_empty_setup(service):
    """A damaged active document remains recoverable instead of silently becoming an empty valid configuration."""
    damaged = {"stores": "damaged"}
    service.documents.write("thermal-state.json", damaged)
    restored = ThermalService(service.documents, service.consumers, service.appliances, service.ev)
    assert restored.document(NOW)["issue"]
    restored.issue = "A different diagnostic must not unlock the damaged configuration"
    with pytest.raises(ValueError):
        restored.configure({"stores": []}, catalogue())
    assert service.documents.read("thermal-state.json") == damaged


def test_terminal_shortfall_uses_frozen_original_target_instead_of_current_temperature(service):
    """An original 55-C target ending at 50 C lacks 5*C kWh even after a new 60-C observation arrives."""
    forecasts(service)
    service.receive(report(service), NOW)
    prepared = prepare(service)
    current = prepared.problem.thermal_work[0]
    plan = {
        "thermal_context": {"revision": prepared.thermal_revision},
        "input_snapshot": {
            "problem": {
                "thermal_work": [asdict(current)],
                "intervals": [
                    {"start": step.start.isoformat(), "duration_h": step.duration_h}
                    for step in prepared.problem.intervals
                ],
            }
        },
        "expected_flows": [
            {"thermal": [{"device_id": DEVICE, "power_w": 0, "end_c": 50, "shortfall_kwh": 0}]}
            for _step in prepared.problem.intervals
        ],
    }
    service.receive(report(service, temperature=60), NOW)
    device = service.document(NOW, plan=plan)["devices"][0]
    assert device["actual"]["temperature_c"] == 60
    assert device["ending_target_c"] == 55
    assert device["ending_stock_shortfall_kwh"] == pytest.approx(5 * 100 * 4.186 / 3600)
    assert device["comfort_shortfall_kwh"] == 0


def test_matching_plan_without_this_stores_flow_keeps_all_deficits_unknown(service):
    """A withheld store has no prediction, even when another device's plan shares its configuration revision."""
    plan = {
        "thermal_context": {"revision": service.revision},
        "input_snapshot": {"problem": {"intervals": [{"start": NOW.isoformat(), "duration_h": 1}]}},
        "expected_flows": [{"thermal": []}],
    }
    device = service.document(NOW, plan=plan)["devices"][0]
    assert device["unmet_draw_kwh"] is None
    assert device["comfort_shortfall_kwh"] is None
    assert device["ending_stock_shortfall_kwh"] is None
    assert device["planned"] == []


def test_telemetry_admission_window_does_not_become_a_five_minute_plan_lifetime(service):
    """Fresh source checks gate current permission, while accepted plan validity retains its normal future bound."""
    from open_energy_planner.optimization.adapters.highs import HighsSolver
    from open_energy_planner.planning import PlanningCoordinator

    forecasts(service)
    service.receive(report(service), NOW)
    original = replace(
        snapshot(), future_valid_until=NOW + timedelta(hours=1), maximum_input_age_s=600
    )
    prepared = service.prepare(original, SimpleNamespace(), NOW, "Europe/Amsterdam")
    assert prepared.problem.thermal_work
    assert prepared.future_valid_until == NOW + timedelta(hours=1)
    result = HighsSolver().solve(prepared.problem)
    assert result.plan is not None, result.stop_reason
    validate(prepared.problem, result.plan)
    published = plan_document(prepared, result, result.plan.costs, NOW)
    assert datetime.fromisoformat(published["valid_until"]) > NOW + timedelta(minutes=5)
    coordinator = PlanningCoordinator(SimpleNamespace(), None)
    coordinator.active = prepared
    ready = prepared.problem.thermal_work[0].ready_at
    assert (
        coordinator.publication_rejection(published, ready - timedelta(seconds=1), prepared) is None
    )
    assert (
        coordinator.publication_rejection(published, ready, prepared)
        == "thermal_dispatch_window_elapsed"
    )
