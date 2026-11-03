"""Independent source-contract, temporal and physical snapshot expectations."""

import asyncio
import logging
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.open_energy_planner.observation_bridge import ObservationBridge
from open_energy_planner.home_assistant.observation_wire import (
    configuration_document,
    parse_configuration,
    parse_report,
)
from open_energy_planner.home_assistant.observations import (
    InputsUnavailable,
    PlanningBasis,
    SnapshotAssembler,
)
from open_energy_planner.installation.model import Installation, InstalledBattery
from open_energy_planner.live_inputs import LiveInputs
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.planning import PlanningCoordinator, PlanningService
from open_energy_planner.solar.calculation.model import SolarConfiguration, SolarInverter
from open_energy_planner.synthetic import capture
from tests.app.forecast.support.basis import forecast_basis
from tests.app.installation.support.problems import installation_problem

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def configuration(*, battery=True, optional=False):
    """Build explicit typed sensor bindings with or without battery measurements."""
    bindings = [{"key": "grid.power", "entity_id": "sensor.grid", "quantity": "power"}]
    if battery:
        bindings.extend(
            [
                {"key": "battery.soc", "entity_id": "sensor.soc", "quantity": "soc"},
                {"key": "battery.power", "entity_id": "sensor.battery", "quantity": "power"},
            ]
        )
    if optional:
        bindings.append(
            {"key": "pv.power", "entity_id": "sensor.pv", "quantity": "power", "required": False}
        )
    return parse_configuration({"bindings": bindings}, revision="sources-1")


def report_body(config=None, *, now=NOW, soc=50):
    """Build normalized observation JSON with explicit source identities and sample/report times."""
    config = config or configuration()
    return {
        "config_revision": config.revision,
        "captured_at": now.isoformat(),
        "issues": [],
        "readings": [
            {
                "key": spec.key,
                "entity_id": spec.entity_id,
                "value": soc if spec.quantity == "soc" else 0,
                "unit": "%" if spec.quantity == "soc" else "W",
                "observed_at": now.isoformat(),
                "reported_at": now.isoformat(),
                "timestamp_kind": "ha_report",
            }
            for spec in config.bindings
        ],
    }


def basis(config=None, *, now=NOW, battery=True):
    """Provide independently versioned future forecasts/prices and configured physical limits for tests."""
    config = config or configuration(battery=battery)
    problem = capture(now).problem
    if not battery:
        problem = replace(problem, batteries=())
    return PlanningBasis(
        config.revision, "forecast-1", "prices-1", now + timedelta(hours=1), problem
    )


@pytest.mark.parametrize(
    "change",
    [
        {"entity_id": "switch.grid"},
        {"quantity": "temperature"},
        {"multiplier": True},
        {"required": "yes"},
        {"maximum_age_s": float("inf")},
        {"maximum_age_s": True},
        {"maximum_age_s": 0},
        {"maximum_age_s": 3601},
        {"timestamp_attribute": []},
        {"key": "battery.soc", "quantity": "power"},
    ],
)
def test_source_configuration_rejects_ambiguous_contracts(change):
    """Corrupt source roles or flags. Require strict configuration rejection instead of ambiguous readiness semantics."""
    raw = configuration_document(configuration(battery=False))
    raw["bindings"][0].update(change)
    with pytest.raises((ValueError, TypeError)):
        parse_configuration(raw)


@pytest.mark.parametrize("role", ["household.power", "pv.power"])
@pytest.mark.parametrize("quantity", ["soc", "energy"])
def test_named_power_roles_reject_other_quantities(role, quantity):
    """Bind a named power role to percent or energy. Reject incompatible source evidence at the app boundary."""
    raw = configuration_document(configuration(battery=False))
    raw["bindings"].append(
        {"key": role, "entity_id": "sensor.extra", "quantity": quantity, "required": False}
    )
    with pytest.raises(ValueError, match="incompatible quantity"):
        parse_configuration(raw)


@pytest.mark.parametrize(
    "change",
    [
        {"value": True},
        {"value": float("nan")},
        {"unit": "kW"},
        {"entity_id": "sensor.other"},
        {"timestamp_kind": "device_sample"},
        {"observed_at": "2026-10-20T12:00:01+00:00"},
        {"reported_at": "2026-10-20T12:00:00"},
    ],
)
def test_wire_receiver_independently_rejects_invalid_observations(change):
    """Corrupt normalized values, units or timestamps. Require app-side rejection without trusting HA normalization alone."""
    raw = report_body()
    raw["readings"][0].update(change)
    with pytest.raises((ValueError, TypeError)):
        parse_report(raw, configuration(), NOW)


def test_report_requires_every_bound_source_exactly_once():
    """Omit or duplicate a selected source. Reject incomplete or ambiguous capture reports."""
    for mutate in (
        lambda observation_body: observation_body["readings"].pop(),
        lambda observation_body: observation_body["readings"].append(
            deepcopy(observation_body["readings"][0])
        ),
    ):
        body = report_body()
        mutate(body)
        with pytest.raises(ValueError):
            parse_report(body, configuration(), NOW)


def measured_installation():
    """Exercise the real owner of measured per-device state with independent cached forecasts."""
    settings = Installation(
        "sensor.grid",
        4000,
        4000,
        (SolarInverter("inverter", 4000, 1),),
        (InstalledBattery("battery", "inverter", "sensor.soc", "sensor.battery", 1, 4000, 4000),),
    )
    solar = SimpleNamespace(
        store=SimpleNamespace(issue=None), configuration=SolarConfiguration((), ()), forecast=None
    )
    return forecast_basis(NOW, solar, settings=settings)


def test_forecast_fixture_export_meters_pass_wire_validation_and_reach_snapshot():
    """Configured cumulative meters retain 123.5 and 7.25 kWh beside percent SoC and watt power evidence."""
    source, _, _ = measured_installation()
    settings = replace(
        source.store.current,
        grid_export_entity="sensor.export_total",
        additional_grid_export_entities=("sensor.export_extra",),
    )
    source, config, report = forecast_basis(NOW, source.solar, settings=settings)
    export_meter_kwh = {"grid.export_energy": 123.5, "grid.export_energy.1": 7.25}
    body = {
        "config_revision": config.revision,
        "captured_at": NOW.isoformat(),
        "readings": [
            {
                **asdict(reading),
                "value": export_meter_kwh.get(reading.key, reading.value),
                "observed_at": reading.observed_at.isoformat(),
                "reported_at": reading.reported_at.isoformat(),
                "timestamp_kind": "ha_state",
            }
            for reading in report.readings
        ],
        "issues": [],
        "location": asdict(report.location),
    }
    report = parse_report(body, config, NOW)
    future = source.current(config, NOW, report=report)
    snapshot = SnapshotAssembler().build(config, report, future, NOW)
    evidence = {reading.key: reading for reading in snapshot.measurement_evidence}
    for key, value in export_meter_kwh.items():
        assert (evidence[key].value, evidence[key].unit) == (value, "kWh")
    assert evidence["battery.battery.soc"].unit == "%"
    assert evidence["battery.battery.power"].unit == "W"
    assert evidence["grid.power"].unit == "W"


def test_real_soc_replaces_only_initial_energy_and_retains_independent_versions():
    """Real InstallationBasisSource maps 75% to .75 kWh and preserves independent forecast provenance."""
    source, config, report = measured_installation()
    report = replace(
        report,
        readings=tuple(
            replace(reading, value=75) if reading.key == "battery.battery.soc" else reading
            for reading in report.readings
        ),
    )
    future = source.current(config, NOW, report=report)
    snapshot = SnapshotAssembler().build(config, report, future, NOW)
    assert snapshot.problem.batteries[0].battery.initial_kwh == 0.75
    assert snapshot.problem.intervals == future.problem.intervals
    assert snapshot.measurement_version == report.version
    assert snapshot.forecast_version == future.forecast_version
    assert snapshot.price_version == future.price_version
    assert snapshot.source == "home_assistant"
    assert snapshot.measurement_evidence[0].value == 0
    assert snapshot.record()["measurement_evidence"][1]["entity_id"] == "sensor.soc"


@pytest.mark.parametrize(
    "change", ["missing_soc", "missing_power", "optional", "entity", "unit", "invalid_soc"]
)
def test_installation_source_requires_valid_bound_per_device_battery_measurements(change):
    """Reject missing, optional, mismatched and physically invalid battery evidence at its real owner."""
    source, config, report = measured_installation()
    key = "battery.battery.power" if change == "missing_power" else "battery.battery.soc"
    if change.startswith("missing"):
        report = replace(report, readings=tuple(row for row in report.readings if row.key != key))
    elif change == "optional":
        config = replace(
            config,
            bindings=tuple(
                replace(row, required=False) if row.key == key else row for row in config.bindings
            ),
        )
    else:
        values = {
            "entity": {"entity_id": "sensor.other"},
            "unit": {"unit": "W"},
            "invalid_soc": {"value": 101},
        }[change]
        report = replace(
            report,
            readings=tuple(
                replace(row, **values) if row.key == key else row for row in report.readings
            ),
        )
    reason = (
        "battery_state_outside_configured_bounds"
        if change == "invalid_soc"
        else "battery_sources_required"
    )
    with pytest.raises(InputsUnavailable, match=reason):
        source.current(config, NOW, report=report)


def test_current_power_is_not_replicated_as_a_future_load_forecast():
    """Change present power independently. Retain configured future demand because current measurements are not forecasts."""
    config = configuration()
    body = report_body()
    body["readings"][0]["value"] = 9876
    snapshot = SnapshotAssembler().build(
        config, parse_report(body, config, NOW), basis(config), NOW
    )
    assert all(step.load_kwh == 0 for step in snapshot.problem.intervals)
    with pytest.raises(InputsUnavailable, match="forecast_or_prices_unavailable"):
        SnapshotAssembler().build(config, parse_report(body, config, NOW), None, NOW)


def test_installation_without_battery_has_no_soc_requirement():
    """Configure a battery-free installation. Assemble valid input without inventing a mandatory SoC source."""
    config = configuration(battery=False)
    report = parse_report(report_body(config), config, NOW)
    snapshot = SnapshotAssembler().build(config, report, basis(config, battery=False), NOW)
    assert snapshot.problem.batteries == ()


def test_battery_free_snapshot_is_accepted_by_the_production_worker():
    """Send a valid battery-free snapshot through the real worker. Accept the candidate without phantom storage."""
    from open_energy_planner.optimization.adapters.process.worker import ProcessSolver

    async def scenario():
        config = configuration(battery=False)
        report = parse_report(report_body(config), config, NOW)
        snapshot = SnapshotAssembler().build(config, report, basis(config, battery=False), NOW)
        snapshot = replace(
            snapshot, problem=replace(capture(NOW).problem, batteries=(), inverters=())
        )
        outcome = await PlanningService(ProcessSolver(), clock=lambda: NOW).calculate(snapshot)
        assert outcome.reason == "validated"
        assert all(step["devices"] == {} for step in outcome.document["steps"])

    asyncio.run(scenario())


def test_optional_fault_does_not_block_but_required_fault_does():
    """Report optional then required source faults. Preserve readiness only while all required measurements remain usable."""
    config = configuration(optional=True)
    body = report_body(config)
    raw = body["readings"].pop()
    body["issues"].append(
        {"key": raw["key"], "entity_id": raw["entity_id"], "reason": "unavailable"}
    )
    assembler = SnapshotAssembler()
    assembler.build(config, parse_report(body, config, NOW), basis(config), NOW)
    raw = body["readings"].pop()
    body["issues"].append(
        {"key": raw["key"], "entity_id": raw["entity_id"], "reason": "unavailable"}
    )
    with pytest.raises(InputsUnavailable, match="required_measurement_unavailable"):
        assembler.build(config, parse_report(body, config, NOW), basis(config), NOW)


def test_explicit_sample_metadata_does_not_shorten_current_capture_validity():
    """A sample timestamp at 299 seconds remains provenance. Its current HA capture stays valid for five minutes."""
    config = configuration()
    config = replace(
        config,
        bindings=(
            config.bindings[0],
            replace(config.bindings[1], timestamp_attribute="sampled_at"),
            *config.bindings[2:],
        ),
    )
    body = report_body()
    body["readings"][1]["timestamp_kind"] = "device_sample"
    body["readings"][1]["observed_at"] = (NOW - timedelta(seconds=299)).isoformat()
    report = parse_report(body, config, NOW)
    snapshot = SnapshotAssembler().build(config, report, basis(config), NOW)
    assert snapshot.maximum_input_age_s == 300
    SnapshotAssembler().build(config, report, basis(config), NOW + timedelta(seconds=1))
    with pytest.raises(InputsUnavailable, match="report_expired"):
        SnapshotAssembler().build(config, report, basis(config), NOW + timedelta(minutes=5))


def test_current_capture_of_unchanged_ha_values_expires_when_bridge_stops():
    """Old sensor timestamps survive intact while the new complete capture has exactly five minutes of validity."""
    config = configuration()
    body = report_body()
    for reading in body["readings"]:
        reading["observed_at"] = reading["reported_at"] = (NOW - timedelta(hours=8)).isoformat()
    report = parse_report(body, config, NOW)
    snapshot = SnapshotAssembler().build(config, report, basis(config), NOW)
    assert snapshot.maximum_input_age_s == 300
    assert all(
        reading.observed_at == NOW - timedelta(hours=8) for reading in snapshot.measurement_evidence
    )
    with pytest.raises(InputsUnavailable, match="report_expired"):
        SnapshotAssembler().build(config, report, basis(config), NOW + timedelta(minutes=5))


def test_unchanged_multiple_export_meters_have_a_current_combined_counter_view():
    """Two unchanged export totals sum to 130.75 kWh without altering original HA report timestamps."""
    from open_energy_planner.contracts.export_meter import export_reading

    config = configuration(battery=False)
    config = replace(
        config,
        bindings=(
            *config.bindings,
            *parse_configuration(
                {
                    "bindings": [
                        {"key": "grid.power", "entity_id": "sensor.grid", "quantity": "power"},
                        {
                            "key": "grid.export_energy",
                            "entity_id": "sensor.export_a",
                            "quantity": "energy",
                        },
                        {
                            "key": "grid.export_energy.1",
                            "entity_id": "sensor.export_b",
                            "quantity": "energy",
                        },
                    ]
                }
            ).bindings[1:],
        ),
    )
    body = report_body(config)
    for reading, value in zip(body["readings"][1:], (123.5, 7.25), strict=True):
        reading.update(
            value=value,
            unit="kWh",
            observed_at=(NOW - timedelta(hours=8)).isoformat(),
            reported_at=(NOW - timedelta(hours=8)).isoformat(),
        )
    report = parse_report(body, config, NOW)
    aggregate, components = export_reading(config, report, NOW)
    assert aggregate.value == 130.75
    assert aggregate.observed_at == NOW
    assert aggregate.timestamp_kind == "ha_capture"
    assert all(row.reported_at == NOW - timedelta(hours=8) for row in components)
    with pytest.raises(ValueError, match="bonus_export_measurement_expired"):
        export_reading(config, report, NOW + timedelta(minutes=5))


def test_partial_first_forecast_interval_is_clipped_without_planning_past_energy():
    """Start five minutes into a quarter. Retain two-thirds of its forecast energy because only ten minutes remain."""
    config = configuration()
    now = NOW + timedelta(minutes=5)
    future = basis(config)
    first = replace(future.problem.intervals[0], load_kwh=3, pv_kwh=6)
    future = replace(future, problem=installation_problem((first, *future.problem.intervals[1:])))
    snapshot = SnapshotAssembler().build(
        config, parse_report(report_body(now=now), config, now), future, now
    )
    assert snapshot.problem.intervals[0].start == now
    assert snapshot.problem.intervals[0].duration_h == pytest.approx(1 / 6)
    assert snapshot.problem.intervals[0].load_kwh == 2
    assert snapshot.problem.intervals[0].pv_kwh == 4
    assert snapshot.problem.intervals[0].pv_sources == (("pv", 4),)


def test_stable_ha_report_is_fresh_but_old_device_sample_is_not_retimed():
    """Accept current HA states with either report or old sample provenance without inventing new sensor timestamps."""

    class State:
        last_reported = NOW

        def as_dict(self):
            return {
                "entity_id": "sensor.grid",
                "state": "0",
                "last_updated": (NOW - timedelta(days=1)).isoformat(),
                "attributes": {
                    "unit_of_measurement": "W",
                    "sample_time": (NOW - timedelta(days=1)).isoformat(),
                },
            }

    class States:
        def get(self, _entity_id):
            return State()

    raw = configuration_document(configuration(battery=False))
    ready, document = ObservationBridge(States(), raw).capture(NOW)
    assert ready
    assert document["readings"][0]["timestamp_kind"] == "ha_report"
    assert parse_report(document, configuration(battery=False), NOW).readings[0].observed_at == NOW
    raw["bindings"][0]["timestamp_attribute"] = "sample_time"
    ready, document = ObservationBridge(States(), raw).capture(NOW)
    assert ready
    assert document["readings"][0]["timestamp_kind"] == "device_sample"
    assert document["readings"][0]["observed_at"] == (NOW - timedelta(days=1)).isoformat()


def test_invalidating_during_solve_discards_candidate_and_allows_recovery(tmp_path):
    """Invalidate a delayed calculation. Publish nothing until recovery submits a newly accepted current generation."""

    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        published = []

        class Solver:
            async def solve(self, problem):
                started.set()
                await finish.wait()
                return HighsSolver().solve(problem)

        async def publish(document):
            published.append(document)

        planner = PlanningCoordinator(PlanningService(Solver(), clock=lambda: NOW), publish)
        config = configuration()
        snapshot = SnapshotAssembler().build(
            config, parse_report(report_body(), config, NOW), basis(config), NOW
        )
        snapshot = replace(
            snapshot,
            problem=replace(
                capture(NOW).problem,
                batteries=(
                    replace(
                        capture(NOW).problem.batteries[0],
                        battery=snapshot.problem.batteries[0].battery,
                    ),
                ),
            ),
        )
        planner.submit(snapshot)
        await started.wait()
        planner.invalidate("source_unavailable")
        finish.set()
        await planner.wait()
        assert not published
        assert planner.status == "not_ready"
        planner.submit(snapshot)
        await planner.wait()
        assert len(published) == 1
        assert published[0]["input_snapshot"]["measurement_version"] == snapshot.measurement_version
        assert datetime.fromisoformat(published[0]["valid_until"]) == NOW + timedelta(minutes=20)
        await planner.close()

    asyncio.run(scenario())


def test_forwarding_reports_does_not_replan_on_every_poll():
    """Forward fresh captures throughout a quarter. Freshness deadlines must not cause extra solves."""

    class Planner:
        latest = None

        def __init__(self):
            self.submitted = []

        def submit(self, snapshot):
            self.submitted.append(snapshot)

        def invalidate(self, _reason):
            self.latest = None

    class Repository:
        def invalidate_plan(self):
            pass

        def store_sources(self, _configuration):
            pass

    class Future:
        def current(self, config, _now, *, report=None):
            return basis(config)

    clock = [NOW]
    planner = Planner()
    live = LiveInputs(planner, Repository(), Future(), clock=lambda: clock[0])
    config = configuration()
    live.configure(config)
    for seconds in (0, 5, 10, 59, 60, 120, 299, 300, 600, 899, 900):
        clock[0] = NOW + timedelta(seconds=seconds)
        live.receive(parse_report(report_body(now=clock[0]), config, clock[0]))
        assert len(planner.submitted) == (2 if seconds == 900 else 1)
        assert live.current_snapshot.measured_at == clock[0]
    assert len(planner.submitted) == 2
    # A quarter transition remains legitimate immediately after explicit configuration.
    live.configure(config)
    for seconds in (1799, 1800):
        clock[0] = NOW + timedelta(seconds=seconds)
        live.receive(parse_report(report_body(now=clock[0]), config, clock[0]))
        assert len(planner.submitted) == (3 if seconds == 1799 else 4)


def test_live_candidate_cannot_skip_first_transfer_when_solving_crosses_boundary():
    """Finish a live solve at its first step's end. Reject it because subsequent stock assumes the skipped transfer."""

    async def scenario():
        clock = [NOW + timedelta(minutes=14, seconds=59)]
        config = configuration()
        snapshot = SnapshotAssembler().build(
            config,
            parse_report(report_body(now=clock[0]), config, clock[0]),
            basis(config),
            clock[0],
        )
        assert snapshot.maximum_input_age_s == 300
        assert snapshot.current_at(clock[0])

        class Solver:
            async def solve(self, problem):
                """Return a real solution after advancing the clock beyond the first transfer's usable window."""
                result = HighsSolver().solve(problem)
                clock[0] = NOW + timedelta(minutes=15)
                return result

        published = []

        async def publish(document):
            """Record publication so the test can detect any unsafe candidate admission."""
            published.append(document)

        planner = PlanningCoordinator(PlanningService(Solver(), clock=lambda: clock[0]), publish)
        planner.submit(snapshot)
        await planner.wait()
        assert planner.reason == "input_expired_during_solve"
        assert not published
        await planner.close()

    asyncio.run(scenario())


def test_changing_price_versions_cannot_bypass_five_minute_limit(caplog):
    """Even a new composed price version every poll coalesces until 300 seconds. Quarters remain immediate."""

    class Planner:
        def __init__(self):
            self.submitted = []

        def submit(self, snapshot):
            self.submitted.append(snapshot)

        def invalidate(self, reason):
            pass

    class Repository:
        def invalidate_plan(self):
            pass

        def store_sources(self, value):
            pass

    class Future:
        def current(self, config, now, *, report=None):
            return replace(basis(config, now=now), price_version=str(now))

    clock = [NOW]
    caplog.set_level(logging.INFO, logger="open_energy_planner.live_inputs")
    planner = Planner()
    live = LiveInputs(planner, Repository(), Future(), clock=lambda: clock[0])
    config = configuration()
    live.configure(config)
    for seconds, expected in (
        (0, 1),
        (5, 1),
        (10, 1),
        (299, 1),
        (300, 2),
        (301, 2),
        (599, 2),
        (600, 3),
        (899, 3),
        (900, 4),
    ):
        clock[0] = NOW + timedelta(seconds=seconds)
        live.receive(parse_report(report_body(now=clock[0]), config, clock[0]))
        assert len(planner.submitted) == expected
    requests = [message for message in caplog.messages if message.startswith("Planning requested:")]
    assert len(requests) == 4
    assert "all required inputs are ready" in requests[0]
    assert "updated market prices" in requests[1]
    assert "300 seconds ago" in requests[1]
    assert "a new quarter-hour started" in requests[-1]
    assert "Last submission" not in caplog.text


@pytest.mark.parametrize(
    ("soc", "power", "expected"),
    [(62.5, 0, False), (67.49, 0, False), (67.5, 0, True), (62.5, 1000, True)],
)
@pytest.mark.parametrize("split_grid", [False, True])
def test_deviation_uses_planned_stock_not_previous_soc(soc, power, expected, split_grid):
    """Halfway from 50% to 75%, 62.5% is normal. A 5-point error or 1kW grid error is material."""
    from types import SimpleNamespace

    original = capture(NOW, initial_kwh=0.5)
    current_time = NOW + timedelta(minutes=7, seconds=30)
    current = replace(
        original,
        measured_at=current_time,
        problem=replace(
            original.problem,
            batteries=(
                replace(
                    original.problem.batteries[0],
                    battery=replace(original.problem.batteries[0].battery, initial_kwh=soc / 100),
                ),
            ),
        ),
    )
    document = {
        "expected_flows": [
            {
                "batteries": [{"device_id": "battery", "end_kwh": 0.75}],
                "grid_import": 0,
                "grid_export": 0,
            }
        ]
        * len(original.problem.intervals)
    }
    planner = SimpleNamespace(published=(original, document))
    live = LiveInputs(planner, None)
    config = configuration()
    if split_grid:
        config = replace(
            config,
            bindings=(
                *config.bindings,
                replace(config.bindings[0], key="grid.power.1", entity_id="sensor.grid2"),
            ),
        )
    body = report_body(config, now=current_time)
    body["readings"][0]["value"] = power - 2000 if split_grid else power
    if split_grid:
        body["readings"][-1]["value"] = 2000
    live.report = parse_report(body, config, current_time)
    assert live.deviated(current, current_time) is expected


def test_command_grace_cannot_outlive_real_forecast_expiry():
    """Fresh five-minute readings permit next-quarter grace, but actual future-data expiry at minute eight wins."""
    from open_energy_planner.planning import CommandMapper

    config = configuration()
    future = replace(basis(config), valid_until=NOW + timedelta(minutes=8))
    snapshot = SnapshotAssembler().build(
        config, parse_report(report_body(), config, NOW), future, NOW
    )
    assert snapshot.maximum_input_age_s == 300
    snapshot = replace(
        snapshot,
        problem=replace(
            capture(NOW).problem,
            batteries=(
                replace(
                    capture(NOW).problem.batteries[0], battery=snapshot.problem.batteries[0].battery
                ),
            ),
        ),
    )
    result = HighsSolver().solve(snapshot.problem)
    document = CommandMapper().build(snapshot, result, result.plan.costs, NOW)
    assert datetime.fromisoformat(document["valid_until"]) == NOW + timedelta(minutes=8)


@pytest.mark.parametrize(
    ("second_kwh", "grid_w", "expected"),
    [(6, 1500, False), (6.99, 2499, False), (7, 1500, True), (6, 2500, True)],
)
def test_installation_deviation_maps_two_independent_battery_trajectories(
    second_kwh, grid_w, expected
):
    """Halfway: 10kWh battery 5 to 7.5 predicts 6.25. 20kWh battery 8 to 4 predicts 6.

    Grid (.5-.125)/.25*1000 is 1500W. Reordered device IDs must preserve these
    trajectories: a 1kWh second-battery error is five points. 1000W grid error is material.
    """
    from types import SimpleNamespace

    from open_energy_planner.installation.model import BatteryState, InstallationProblem
    from open_energy_planner.optimization.domain import Battery, Interval
    from open_energy_planner.solar.calculation.model import SolarInverter

    inputs = InstallationProblem(
        (Interval(NOW, 0.25, 0.2, 0.1),),
        (SolarInverter("inv-a", 4000, 1, True), SolarInverter("inv-b", 4000, 1, True)),
        (BatteryState("a", "inv-a", Battery(10, 5)), BatteryState("b", "inv-b", Battery(20, 8))),
        (),
        10000,
        10000,
    )
    original = replace(capture(NOW), problem=inputs)
    halfway = NOW + timedelta(minutes=7, seconds=30)
    measured = tuple(
        replace(
            unit,
            battery=replace(
                unit.battery, initial_kwh=6.25 if unit.device_id == "a" else second_kwh
            ),
        )
        for unit in reversed(inputs.batteries)
    )
    current = replace(original, measured_at=halfway, problem=replace(inputs, batteries=measured))
    document = {
        "expected_flows": [
            {
                "grid_import": 0.5,
                "grid_export": 0.125,
                "batteries": [{"device_id": "a", "end_kwh": 7.5}, {"device_id": "b", "end_kwh": 4}],
            }
        ]
    }
    live = LiveInputs(SimpleNamespace(published=(original, document)), None)
    body = report_body(now=halfway)
    body["readings"][0]["value"] = grid_w
    live.report = parse_report(body, configuration(), halfway)
    assert live.deviated(current, halfway) is expected
