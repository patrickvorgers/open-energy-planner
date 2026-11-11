"""Measured calibration evidence, deadline policy and independent physical dispatch checks."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from open_energy_planner.installation.calibration import BatteryCalibration
from open_energy_planner.installation.model import (
    BatteryState,
    Installation,
    InstallationProblem,
    InstalledBattery,
)
from open_energy_planner.installation.storage import parse
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import Battery, Interval, Status
from open_energy_planner.solar.calculation.model import SolarInverter
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def problem(*, mandatory=False, count=2, grid=4000):
    """Specify lossless quarters independently: 4 kW stores 1 kWh in each quarter."""
    return InstallationProblem(
        tuple(
            Interval(NOW + timedelta(minutes=15 * index), 0.25, 10 if index == 0 else 0, 0)
            for index in range(count)
        ),
        (SolarInverter("inv", 4000, 1),),
        (
            BatteryState(
                "battery",
                "inv",
                Battery(2, 0, minimum_kwh=1, charge_limit_w=4000, recover_reserve=mandatory),
            ),
        ),
        (),
        grid,
        4000,
    )


@pytest.fixture
def tracker():
    """Use real durable JSON documents without importing a Home Assistant framework."""
    engine = create_engine("sqlite://")
    SCHEMA.create_all(engine)
    ledger = BatteryCalibration(SqlDocuments(engine, "installation"))
    yield ledger
    engine.dispose()


def configured(tracker, **changes):
    """Enable one independent 2 kWh battery with an explicit synthetic clock."""
    battery = InstalledBattery(
        "battery",
        "inv",
        "sensor.soc",
        "sensor.power",
        2,
        4000,
        4000,
        calibration_enabled=True,
        **changes,
    )
    definition = Installation("sensor.grid", 4000, 4000, problem().inverters, (battery,))
    rows = tracker.configured(definition, NOW)
    tracker.write(rows)
    tracker.install(rows)
    return definition, battery


def observation(at, soc=100, power=0, *, present=True):
    """Provide normalized terminal discharge-positive captures, never predicted plan energy."""
    readings = (
        tuple(
            SimpleNamespace(key=f"battery.battery.{key}", entity_id=entity, value=value, unit=unit)
            for key, entity, value, unit in (
                ("soc", "sensor.soc", soc, "%"),
                ("power", "sensor.power", power, "W"),
            )
        )
        if present
        else ()
    )
    return SimpleNamespace(captured_at=at, readings=readings)


def continuous_hold(tracker, settings, start, *, duration_minutes=180, soc=98):
    """Bridge the whole hold with five-minute captures, retaining all continuity checks."""
    tracker.observe(settings, observation(start))
    for minute in range(5, duration_minutes + 1, 5):
        tracker.observe(settings, observation(start + timedelta(minutes=minute), soc=soc))


def test_defaults_and_disabled_record_preserve_existing_settings():
    """Calibration is opt-in, defaults to 14 days/3 hours and does not change old disabled hashes."""
    battery = InstalledBattery("battery", "inv", "sensor.soc", "sensor.power", 2, 4000, 4000)
    settings = Installation("sensor.grid", 4000, 4000, problem().inverters, (battery,))
    assert not battery.calibration_enabled
    assert battery.calibration_interval_days == 14
    assert battery.calibration_hold_hours == 3
    assert not any(key.startswith("calibration") for key in settings.record()["batteries"][0])
    assert parse(json.loads(json.dumps(settings.record()))) == settings


@pytest.mark.parametrize(
    "changes",
    [
        {"calibration_enabled": 1},
        {"calibration_interval_days": 0},
        {"calibration_interval_days": 14.5},
        {"calibration_interval_days": True},
        {"calibration_interval_days": 366},
        {"calibration_hold_hours": 0},
        {"calibration_hold_hours": float("nan")},
        {"calibration_hold_hours": 25},
        {"charge_limit_w": 0, "calibration_enabled": True},
    ],
)
def test_invalid_calibration_settings_are_rejected(changes):
    """Only bounded explicit policies with charging capability can enter the installation."""
    fields = {
        "device_id": "battery",
        "inverter_id": "inv",
        "soc_entity": "sensor.soc",
        "power_entity": "sensor.power",
        "capacity_kwh": 2,
        "charge_limit_w": 4000,
        "discharge_limit_w": 4000,
    }
    with pytest.raises((TypeError, ValueError)):
        InstalledBattery(**{**fields, **changes})


def test_measured_hold_accepts_absorption_and_advances_deadline(tracker):
    """100% followed by 3h at 98% and zero discharge completes, with the next deadline exactly 14 days later."""
    settings, battery = configured(tracker)
    continuous_hold(tracker, settings, NOW)
    finished = NOW + timedelta(hours=3)
    assert tracker.state["battery"]["completed_at"] == finished.isoformat()
    assert tracker.due_at(battery) == finished + timedelta(days=14)
    assert tracker.state["battery"]["hold_started_at"] is None
    restored = BatteryCalibration(tracker.documents)
    assert restored.state == tracker.state


@pytest.mark.parametrize(
    "soc,power,present,gap",
    [(97.9, 0, True, 5), (99, 1, True, 5), (99, 0, False, 5), (99, 0, True, 6)],
)
def test_interrupted_hold_never_counts_as_completion(tracker, soc, power, present, gap):
    """Too much sag, any active discharge, absent inputs and a capture gap each invalidate elapsed hold time."""
    settings, _battery = configured(tracker)
    tracker.observe(settings, observation(NOW))
    tracker.observe(
        settings, observation(NOW + timedelta(minutes=gap), soc, power, present=present)
    )
    assert tracker.state["battery"]["hold_started_at"] is None
    assert tracker.state["battery"]["completed_at"] is None


def test_restart_gap_does_not_invent_unobserved_completion(tracker):
    """A persisted full reading followed by a three-hour outage must reacquire 100% and the full hold."""
    settings, _battery = configured(tracker)
    tracker.observe(settings, observation(NOW))
    restored = BatteryCalibration(tracker.documents)
    restored.observe(settings, observation(NOW + timedelta(hours=3), soc=99))
    assert restored.state["battery"]["completed_at"] is None
    assert restored.state["battery"]["hold_started_at"] is None


def test_reordered_capture_cannot_erase_progress(tracker):
    """An older report cannot restart or complete a newer observed hold."""
    settings, _battery = configured(tracker)
    tracker.observe(settings, observation(NOW + timedelta(minutes=5)))
    original = tracker.state.copy()
    tracker.observe(settings, observation(NOW, soc=10, power=4000))
    assert tracker.state == original


def test_policy_starts_before_deadline_and_does_not_complete_from_a_plan(tracker):
    """A 2 kWh/4 kW battery reserves one rounded charging hour plus 3h hold and a quarter-hour margin."""
    _settings, battery = configured(tracker)
    assert tracker.due_at(battery) == NOW + timedelta(days=14)
    assert tracker.directive(battery, NOW + timedelta(days=13, hours=19), 4000) == (False, None)
    assert tracker.directive(battery, NOW + timedelta(days=13, hours=20), 4000) == (True, None)
    assert tracker.document(battery, NOW + timedelta(days=15))["status"] == "overdue"
    assert tracker.state["battery"]["completed_at"] is None


def test_unrelated_setting_preserves_progress_and_disable_clears_it(tracker):
    """Changing grid ratings preserves evidence, while disabling removes the old deadline before re-enabling."""
    settings, battery = configured(tracker)
    continuous_hold(tracker, settings, NOW)
    assert tracker.configured(replace(settings, import_limit_w=3000), NOW) == tracker.state
    disabled = replace(settings, batteries=(replace(battery, calibration_enabled=False),))
    assert tracker.configured(disabled, NOW) == {}


@pytest.mark.planner_scenario
def test_calibration_charge_precedes_prices_and_never_discharges():
    """Even with expensive first-quarter import, calibration fills 2 kWh at 4 kW before money, without discharging."""
    original = problem(mandatory=False, count=4)
    inputs = replace(original, batteries=(replace(original.batteries[0], calibration_charge=True),))
    result = HighsSolver().solve(inputs)
    assert result.status == Status.OPTIMAL, result
    validate(inputs, result.plan)
    assert result.plan.flows[0].batteries[0].charge_kwh == pytest.approx(1)
    assert result.plan.flows[1].batteries[0].end_kwh >= 1.999
    assert all(flow.batteries[0].discharge_kwh == 0 for flow in result.plan.flows)


@pytest.mark.planner_scenario
def test_hold_waits_for_measured_completion_before_releasing():
    """An elapsed predicted deadline cannot release discharge. A new cleared measured state can."""
    original = problem(mandatory=False, count=4)
    start = original.intervals[0].start
    unit = replace(
        original.batteries[0],
        battery=replace(original.batteries[0].battery, initial_kwh=2),
        calibration_hold_until=start + timedelta(minutes=30),
    )
    inputs = replace(
        original,
        batteries=(unit,),
        intervals=tuple(replace(step, load_kwh=0.5, buy_price=10) for step in original.intervals),
    )
    result = HighsSolver().solve(inputs)
    assert result.status == Status.OPTIMAL, result
    validate(inputs, result.plan)
    assert all(flow.batteries[0].discharge_kwh == 0 for flow in result.plan.flows)
    released = replace(inputs, batteries=(replace(unit, calibration_hold_until=None),))
    normal = HighsSolver().solve(released)
    assert normal.status == Status.OPTIMAL, normal
    validate(released, normal.plan)
    assert sum(
        flow.batteries[0].discharge_kwh - flow.batteries[0].charge_kwh for flow in normal.plan.flows
    ) == pytest.approx(1)
    with pytest.raises(ValueError, match="calibration"):
        validate(inputs, normal.plan)


@pytest.mark.planner_scenario
def test_fractional_last_charge_reaches_capacity_with_bounded_whole_watt_request():
    """A 1.9999 kWh battery accepts 0.0001 kWh, while a 1 W request stops at its real 2 kWh capacity."""
    from open_energy_planner.installation.commands import plan_document
    from open_energy_planner.synthetic import capture

    original = problem(count=1)
    unit = replace(
        original.batteries[0],
        battery=replace(original.batteries[0].battery, initial_kwh=1.9999),
        calibration_charge=True,
    )
    inputs = replace(original, batteries=(unit,))
    result = HighsSolver().solve(inputs)
    assert result.status == Status.OPTIMAL, result
    validate(inputs, result.plan)
    actual = result.plan.flows[0].batteries[0]
    assert actual.charge_kwh == pytest.approx(0.0001)
    assert actual.end_kwh == pytest.approx(2)
    snapshot = replace(capture(now=NOW), problem=inputs)
    command = plan_document(snapshot, result, result.plan.costs, NOW)["steps"][0]["devices"][
        "battery"
    ]
    assert command == {"kind": "battery", "mode": "fixed_power", "power_w": -1}


@pytest.mark.planner_scenario
def test_holding_never_emits_grid_following_and_archived_context_restores_deadline():
    """A zero-flow hold must publish fixed zero power, while archived ISO progress reconstructs as typed state."""
    from open_energy_planner.installation.commands import plan_document
    from open_energy_planner.savings.context import physical_context
    from open_energy_planner.synthetic import capture

    original = problem(count=2)
    deadline = NOW + timedelta(hours=3)
    unit = replace(
        original.batteries[0],
        battery=replace(original.batteries[0].battery, initial_kwh=2),
        can_follow_grid=True,
        calibration_hold_until=deadline,
    )
    inputs = replace(original, batteries=(unit,))
    result = HighsSolver().solve(inputs)
    assert result.status == Status.OPTIMAL, result
    validate(inputs, result.plan)
    snapshot = replace(capture(now=NOW), problem=inputs)
    document = plan_document(snapshot, result, result.plan.costs, NOW)
    assert all(
        step["devices"]["battery"] == {"kind": "battery", "mode": "fixed_power", "power_w": 0}
        for step in document["steps"]
    )
    assert physical_context(document).batteries[0].calibration_hold_until == deadline


def test_one_battery_completion_does_not_complete_another(tracker):
    """Each battery needs its own real full/hold evidence, even when both share an inverter."""
    settings, battery = configured(tracker, calibration_hold_hours=0.25)
    other = replace(
        battery, device_id="other", soc_entity="sensor.other_soc", power_entity="sensor.other_power"
    )
    settings = replace(settings, batteries=(battery, other))
    rows = tracker.configured(settings, NOW)
    tracker.write(rows)
    tracker.install(rows)
    continuous_hold(tracker, settings, NOW, duration_minutes=15)
    assert tracker.state["battery"]["completed_at"] == (NOW + timedelta(minutes=15)).isoformat()
    assert tracker.state["other"]["completed_at"] is None


def test_unreadable_calibration_has_actionable_recovery_advice():
    """Missing durable evidence must name the affected policy and an explicit reset route."""
    from open_energy_planner.runtime.diagnostics.planning_notifications import (
        explain_planning_problem,
    )

    message = explain_planning_problem("battery_calibration_unreadable")
    assert message.action_path == "/setup"
    assert "Installation > Batteries" in message.message
    assert "new calibration schedule" in message.message


def test_normal_captures_checkpoint_once_per_minute(tracker, monkeypatch):
    """Five regular quarter-minute captures update one bounded document only at the first capture and minute boundary."""
    settings, _battery = configured(tracker)
    writes = []
    original = tracker.documents.write

    def record_write(name, body):
        """Count real SQL writes without replacing their durable behavior."""
        writes.append(name)
        original(name, body)

    monkeypatch.setattr(tracker.documents, "write", record_write)
    for second in (0, 15, 30, 45, 60):
        tracker.observe(settings, observation(NOW + timedelta(seconds=second), soc=50))
    assert writes == ["battery-calibration.json", "battery-calibration.json"]


def test_failed_completion_write_cannot_advance_memory_or_restart_state(tracker, monkeypatch):
    """A failed ledger write leaves both in-memory and reopened completion absent, and the same capture can be retried."""
    settings, _battery = configured(tracker, calibration_hold_hours=0.25)
    continuous_hold(tracker, settings, NOW, duration_minutes=10)
    original = tracker.documents.write

    def refuse_write(_name, _body):
        """Inject a storage failure before any success is installed."""
        raise OSError("Synthetic calibration write failure")

    monkeypatch.setattr(tracker.documents, "write", refuse_write)
    final = observation(NOW + timedelta(minutes=15), soc=98)
    with pytest.raises(OSError):
        tracker.observe(settings, final)
    assert tracker.state["battery"]["completed_at"] is None
    assert BatteryCalibration(tracker.documents).state["battery"]["completed_at"] is None
    monkeypatch.setattr(tracker.documents, "write", original)
    tracker.observe(settings, final)
    assert tracker.state["battery"]["completed_at"] == final.captured_at.isoformat()


@pytest.mark.planner_scenario
def test_no_grid_capacity_keeps_real_bounds_and_unfinished_calibration():
    """A disconnected grid cannot provide energy. Calibration must never invent charge or relax limits."""
    original = problem(mandatory=False, count=2, grid=0)
    inputs = replace(original, batteries=(replace(original.batteries[0], calibration_charge=True),))
    result = HighsSolver().solve(inputs)
    assert result.status == Status.OPTIMAL, result
    validate(inputs, result.plan)
    assert all(flow.batteries[0].end_kwh == 0 for flow in result.plan.flows)
