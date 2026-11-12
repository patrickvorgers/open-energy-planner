"""Measured calibration evidence, deadline policy and independent physical dispatch checks."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from open_energy_planner.installation.calibration import BatteryCalibration, available_charge_w
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


def problem(*, mandatory=False, interval_count=2, grid_import_limit_w=4000):
    """Specify lossless quarters independently: 4 kW stores 1 kWh in each quarter."""
    return InstallationProblem(
        tuple(
            Interval(NOW + timedelta(minutes=15 * index), 0.25, 10 if index == 0 else 0, 0)
            for index in range(interval_count)
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
        grid_import_limit_w,
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
            SimpleNamespace(
                key=f"battery.battery.{key}",
                entity_id=entity,
                value=value,
                unit=unit,
                timestamp_kind="ha_state",
                observed_at=at,
            )
            for key, entity, value, unit in (
                ("soc", "sensor.soc", soc, "%"),
                ("power", "sensor.power", power, "W"),
            )
        )
        if present
        else ()
    )
    return SimpleNamespace(captured_at=at, readings=readings)


def wire_observation(settings, at, *, sampled_at, timestamp_kind="device_sample"):
    """Parse real wire/config contracts while varying explicit device sample times independently of fresh captures."""
    from open_energy_planner.home_assistant.observation_wire import (
        parse_configuration,
        parse_report,
    )
    from open_energy_planner.installation.storage import sources

    configuration = parse_configuration(
        {
            "bindings": [
                {
                    "key": binding.key,
                    "entity_id": binding.entity_id,
                    "quantity": binding.quantity,
                    **(
                        {"timestamp_attribute": "sampled_at"}
                        if binding.key.startswith("battery.") and timestamp_kind == "device_sample"
                        else {}
                    ),
                }
                for binding in sources(settings).bindings
            ]
        },
        revision=settings.version,
    )
    return parse_report(
        {
            "config_revision": settings.version,
            "captured_at": at.isoformat(),
            "issues": [],
            "readings": [
                {
                    "key": binding.key,
                    "entity_id": binding.entity_id,
                    "value": 100 if binding.quantity == "soc" else 0,
                    "unit": "%" if binding.quantity == "soc" else "W",
                    "timestamp_kind": timestamp_kind
                    if binding.key.startswith("battery.")
                    else "ha_state",
                    "observed_at": sampled_at.get(binding.key, at).isoformat(),
                    "reported_at": at.isoformat(),
                }
                for binding in configuration.bindings
            ],
        },
        configuration,
        at,
    )


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
        {"calibration_interval_days": 181},
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


@pytest.mark.parametrize("interval_days", [1, 14, 180])
def test_selected_interval_sets_the_actual_deadline(tracker, interval_days):
    """The chosen 1, 14 or 180 days survives serialization and determines when charging becomes due."""
    settings, battery = configured(tracker, calibration_interval_days=interval_days)
    assert parse(json.loads(json.dumps(settings.record()))) == settings
    assert tracker.due_at(battery) == NOW + timedelta(days=interval_days)
    restored = BatteryCalibration(tracker.documents)
    assert restored.issue is None
    assert restored.due_at(battery) == NOW + timedelta(days=interval_days)
    assert tracker.directive(battery, NOW, 4000) == (False, None)
    assert tracker.directive(battery, NOW + timedelta(days=interval_days), 4000) == (True, None)


@pytest.mark.parametrize("timestamp_key", ["enabled_at", "completed_at", "hold_started_at"])
def test_unrepresentable_ledger_deadline_is_unreadable(tracker, timestamp_key):
    """Restoration rejects overflowing schedule and hold deadlines before any UI or planner read."""
    _settings, battery = configured(tracker)
    rows = tracker.configured(_settings, NOW)
    rows["battery"][timestamp_key] = "9999-12-31T23:59:59+00:00"
    if timestamp_key != "enabled_at":
        rows["battery"]["observed_at"] = rows["battery"][timestamp_key]
    tracker.write(rows)
    restored = BatteryCalibration(tracker.documents)
    assert restored.issue == "battery_calibration_unreadable"
    assert restored.document(battery, NOW) == {"status": "unavailable"}


@pytest.mark.planner_scenario
def test_natural_full_charge_enforces_hold_before_charging_window(tracker):
    """A natural full capture prevents costly discharge immediately, thirteen days before scheduled charging."""
    from open_energy_planner.installation.commands import plan_document
    from open_energy_planner.synthetic import capture

    settings, battery = configured(tracker)
    tracker.observe(settings, observation(NOW))
    charge, hold_until = tracker.directive(battery, NOW, 4000)
    original = problem(interval_count=2)
    unit = replace(
        original.batteries[0],
        battery=replace(original.batteries[0].battery, initial_kwh=2),
        can_follow_grid=True,
        calibration_charge=charge,
        calibration_hold_until=hold_until,
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
    published = plan_document(
        replace(capture(now=NOW), problem=inputs), result, result.plan.costs, NOW
    )
    assert all(
        step["devices"]["battery"] == {"kind": "battery", "mode": "fixed_power", "power_w": 0}
        for step in published["steps"]
    )


@pytest.mark.planner_scenario
@pytest.mark.parametrize("enabled", [False, True])
def test_calibration_is_only_planned_when_enabled(tracker, enabled):
    """Past the selected 180-day deadline, only opt-in charges through the expensive first quarter."""
    _settings, battery = configured(tracker, calibration_interval_days=180)
    selected = replace(battery, calibration_enabled=enabled)
    charge, hold_until = tracker.directive(selected, NOW + timedelta(days=181), 4000)
    original = problem(interval_count=4)
    inputs = replace(
        original,
        batteries=(
            replace(
                original.batteries[0], calibration_charge=charge, calibration_hold_until=hold_until
            ),
        ),
    )
    result = HighsSolver().solve(inputs)
    assert result.status == Status.OPTIMAL, result
    validate(inputs, result.plan)
    assert result.plan.flows[0].batteries[0].charge_kwh == pytest.approx(1 if enabled else 0)
    assert hold_until is None


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


@pytest.mark.planner_scenario
def test_completed_hold_does_not_restart_until_next_calibration_cycle(tracker):
    """Remaining at 100% after a measured hold releases dispatch until the next charging window, which can start another hold."""
    settings, battery = configured(tracker)
    continuous_hold(tracker, settings, NOW, soc=100)
    completed = NOW + timedelta(hours=3)
    for seconds in (15, 30, 60):
        tracker.observe(settings, observation(completed + timedelta(seconds=seconds)))
        assert tracker.state["battery"]["hold_started_at"] is None
        assert tracker.state["battery"]["completed_at"] == completed.isoformat()
    tracker = BatteryCalibration(tracker.documents)
    charge, hold_until = tracker.directive(battery, completed + timedelta(minutes=1), 4000)
    original = problem(interval_count=2)
    inputs = replace(
        original,
        batteries=(
            replace(
                original.batteries[0],
                battery=replace(original.batteries[0].battery, initial_kwh=2),
                calibration_charge=charge,
                calibration_hold_until=hold_until,
            ),
        ),
        intervals=tuple(replace(step, load_kwh=0.5, buy_price=10) for step in original.intervals),
    )
    result = HighsSolver().solve(inputs)
    assert result.status == Status.OPTIMAL, result
    validate(inputs, result.plan)
    assert sum(flow.batteries[0].discharge_kwh for flow in result.plan.flows) == pytest.approx(1)
    next_due = completed + timedelta(days=14)
    tracker.observe(settings, observation(next_due - timedelta(hours=4)))
    assert (
        tracker.state["battery"]["hold_started_at"] == (next_due - timedelta(hours=4)).isoformat()
    )
    assert tracker.directive(battery, next_due - timedelta(hours=4), 4000) == (
        False,
        next_due - timedelta(hours=1),
    )


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


@pytest.mark.parametrize("shared_converter", [True, False])
@pytest.mark.planner_scenario
def test_start_time_shares_converter_capacity_between_connected_batteries(
    tracker, shared_converter
):
    """Two 10 kWh batteries require 5 h through one 4 kW inverter, or 3 rounded hours each on separate inverters."""
    settings, original_battery = configured(tracker, charge_efficiency=1)
    battery = replace(original_battery, capacity_kwh=10)
    other = replace(
        battery,
        device_id="other",
        inverter_id="inv" if shared_converter else "other_inv",
        soc_entity="sensor.other_soc",
        power_entity="sensor.other_power",
    )
    converters = (SolarInverter("inv", 4000, 1),)
    if not shared_converter:
        converters += (SolarInverter("other_inv", 4000, 1),)
    settings = replace(
        settings, import_limit_w=10000, inverters=converters, batteries=(battery, other)
    )
    rows = tracker.configured(settings, NOW)
    tracker.write(rows)
    tracker.install(rows)
    expected_hours = 5 if shared_converter else 3
    deadline = NOW + timedelta(days=14)
    start = deadline - timedelta(hours=expected_hours + 3.25)
    for selected in settings.batteries:
        converter = next(unit for unit in converters if unit.inverter_id == selected.inverter_id)
        power_w = available_charge_w(settings, selected, converter)
        assert tracker.directive(selected, start - timedelta(seconds=1), power_w) == (False, None)
        assert tracker.directive(selected, start, power_w) == (True, None)
    if shared_converter:
        inputs = InstallationProblem(
            tuple(Interval(NOW + timedelta(hours=index), 1, 1, 0) for index in range(5)),
            converters,
            tuple(
                BatteryState(
                    unit.device_id,
                    unit.inverter_id,
                    Battery(10, 0, charge_limit_w=4000),
                    calibration_charge=True,
                )
                for unit in settings.batteries
            ),
            (),
            10000,
            10000,
        )
        result = HighsSolver().solve(inputs)
        assert result.status == Status.OPTIMAL, result
        validate(inputs, result.plan)
        assert sum(unit.end_kwh for unit in result.plan.flows[3].batteries) == pytest.approx(16)
        assert [unit.end_kwh for unit in result.plan.flows[4].batteries] == pytest.approx([10, 10])


def test_extreme_charge_duration_starts_immediately_without_datetime_underflow(tracker):
    """An accepted 20,000 kWh battery at 1 W needs 20 million hours and must charge now rather than subtract past year one."""
    settings, original_battery = configured(tracker, charge_efficiency=1)
    battery = replace(original_battery, capacity_kwh=20000, charge_limit_w=1)
    settings = replace(settings, batteries=(battery,))
    rows = tracker.configured(settings, NOW)
    tracker.write(rows)
    tracker.install(rows)
    assert tracker.directive(battery, NOW, 1) == (True, None)
    assert tracker.directive(battery, NOW + timedelta(days=15), 1) == (True, None)
    tracker.observe(settings, observation(NOW))
    assert tracker.directive(battery, NOW, 1) == (False, NOW + timedelta(hours=3))


@pytest.mark.parametrize("stale_key", ["soc", "power"])
def test_stale_device_samples_interrupt_hold_and_fresh_wire_evidence_recovers(tracker, stale_key):
    """Fresh captures cannot complete a 15-minute hold using repeated old device samples from either required source."""
    settings, _battery = configured(tracker, calibration_hold_hours=0.25)

    def wire_capture(at, *, stale=False):
        """Hold one selected device sample fixed while all other evidence advances."""
        samples = {f"battery.battery.{stale_key}": NOW} if stale else {}
        return wire_observation(settings, at, sampled_at=samples)

    for minute in (0, 5, 10, 15):
        tracker.observe(settings, wire_capture(NOW + timedelta(minutes=minute), stale=True))
    assert tracker.state["battery"]["completed_at"] is None
    assert tracker.state["battery"]["hold_started_at"] is None
    for minute in (20, 25, 30, 35):
        tracker.observe(settings, wire_capture(NOW + timedelta(minutes=minute)))
    completed = NOW + timedelta(minutes=35)
    assert tracker.state["battery"]["completed_at"] == completed.isoformat()
    tracker.observe(settings, wire_capture(NOW + timedelta(minutes=40), stale=True))
    assert tracker.state["battery"]["completed_at"] == completed.isoformat()


@pytest.mark.parametrize(
    "timestamp_kind,sample_age_seconds,can_complete",
    [
        ("device_sample", 300, True),
        ("device_sample", 301, False),
        ("ha_state", 28800, True),
        ("ha_report", 28800, True),
    ],
)
def test_sample_age_boundary_preserves_constant_ha_state_captures(
    tracker, timestamp_kind, sample_age_seconds, can_complete
):
    """Device evidence allows at most 300 seconds, while unchanged HA state/report times still complete a real three-hour capture hold."""
    settings, _battery = configured(tracker)
    for minute in range(0, 181, 5):
        at = NOW + timedelta(minutes=minute)
        sample_time = (at if timestamp_kind == "device_sample" else NOW) - timedelta(
            seconds=sample_age_seconds
        )
        report = wire_observation(
            settings,
            at,
            timestamp_kind=timestamp_kind,
            sampled_at={f"battery.battery.{role}": sample_time for role in ("soc", "power")},
        )
        tracker.observe(settings, report)
    assert tracker.state["battery"]["completed_at"] == (
        (NOW + timedelta(hours=3)).isoformat() if can_complete else None
    )
    assert tracker.state["battery"]["hold_started_at"] is None


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
    original = problem(mandatory=False, interval_count=4)
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
    original = problem(mandatory=False, interval_count=4)
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
@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("initial_kwh", [1.9999, 1.9998])
def test_fractional_last_charge_reaches_capacity_with_bounded_whole_watt_request(
    split, initial_kwh
):
    """At a 1 W limit, a 0.0001 kWh deficit fills in the first half and 0.0002 kWh in the second, without changing requests."""
    from open_energy_planner.installation.commands import plan_document
    from open_energy_planner.synthetic import capture

    original = problem(interval_count=1)
    if split:
        original = replace(
            original,
            intervals=tuple(
                replace(
                    original.intervals[0],
                    start=NOW + timedelta(minutes=7.5 * index),
                    duration_h=0.125,
                    control_group="quarter",
                )
                for index in range(2)
            ),
        )
    unit = replace(
        original.batteries[0],
        battery=replace(original.batteries[0].battery, initial_kwh=initial_kwh, charge_limit_w=1),
        calibration_charge=True,
    )
    inputs = replace(original, batteries=(unit,))
    result = HighsSolver().solve(inputs)
    assert result.status == Status.OPTIMAL, result
    validate(inputs, result.plan)
    actual = result.plan.flows[0].batteries[0]
    deficit_kwh = 2 - initial_kwh
    assert actual.charge_kwh == pytest.approx(min(deficit_kwh, 0.000125) if split else deficit_kwh)
    assert result.plan.flows[-1].batteries[0].end_kwh == pytest.approx(2)
    assert sum(flow.batteries[0].charge_kwh for flow in result.plan.flows) == pytest.approx(
        deficit_kwh
    )
    snapshot = replace(capture(now=NOW), problem=inputs)
    steps = plan_document(snapshot, result, result.plan.costs, NOW)["steps"]
    assert len(steps) == 1
    assert steps[0]["end"] == (NOW + timedelta(minutes=15)).isoformat()
    command = steps[0]["devices"]["battery"]
    assert command == {"kind": "battery", "mode": "fixed_power", "power_w": -1}


@pytest.mark.planner_scenario
def test_capacity_clipping_cannot_mask_a_request_above_terminal_limit():
    """A 0.4 W accepted mean at full still requires 1 W, which independent validation rejects against a 0.5 W limit."""
    original = problem(interval_count=1)
    unit = replace(
        original.batteries[0],
        calibration_charge=True,
        battery=replace(original.batteries[0].battery, initial_kwh=1.9999, charge_limit_w=1),
    )
    inputs = replace(original, batteries=(unit,))
    result = HighsSolver().solve(inputs)
    assert result.status == Status.OPTIMAL, result
    validate(inputs, result.plan)
    assert result.plan.flows[0].batteries[0].charge_kwh == pytest.approx(0.0001)
    limited = replace(
        inputs, batteries=(replace(unit, battery=replace(unit.battery, charge_limit_w=0.5)),)
    )
    with pytest.raises(ValueError, match="Whole-watt calibration charge limit"):
        validate(limited, result.plan)


@pytest.mark.planner_scenario
def test_holding_never_emits_grid_following_and_archived_context_restores_deadline():
    """A zero-flow hold must publish fixed zero power, while archived ISO progress reconstructs as typed state."""
    from open_energy_planner.installation.commands import plan_document
    from open_energy_planner.savings.context import physical_context
    from open_energy_planner.synthetic import capture

    original = problem(interval_count=2)
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


@pytest.mark.parametrize("fail_write", [False, True])
def test_unrelated_save_persists_between_checkpoint_observation(tracker, monkeypatch, fail_write):
    """An installation transaction must commit pending hold evidence or leave both durable settings and progress unchanged."""
    from open_energy_planner.installation.storage import InstallationStore

    settings, _battery = configured(tracker)
    store = InstallationStore(tracker.documents)
    store.save(settings)
    start = datetime.now(UTC)
    store.observe(observation(start))
    persisted = store.calibration.persisted.copy()
    store.observe(observation(start + timedelta(seconds=15), soc=98))
    pending = store.calibration.state.copy()
    assert pending != persisted
    changed = replace(settings, import_limit_w=3000)
    if fail_write:
        original_write = tracker.documents.write

        def refuse_progress(name, body):
            """Fail only the ledger write inside the existing settings transaction."""
            if name == "battery-calibration.json":
                raise OSError("Synthetic checkpoint failure")
            original_write(name, body)

        monkeypatch.setattr(tracker.documents, "write", refuse_progress)
        with pytest.raises(OSError):
            store.save(changed)
    else:
        store.save(changed)
    restored = InstallationStore(tracker.documents)
    assert restored.current == (settings if fail_write else changed)
    assert restored.calibration.state == (persisted if fail_write else pending)
    assert store.calibration.persisted == (persisted if fail_write else pending)


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
    original = problem(mandatory=False, interval_count=2, grid_import_limit_w=0)
    inputs = replace(original, batteries=(replace(original.batteries[0], calibration_charge=True),))
    result = HighsSolver().solve(inputs)
    assert result.status == Status.OPTIMAL, result
    validate(inputs, result.plan)
    assert all(flow.batteries[0].end_kwh == 0 for flow in result.plan.flows)
