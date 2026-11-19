"""Independent unoptimized service timing, archive compatibility and chart/cost regressions."""

import json
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.appliances.cycles.model import ApplianceWork, CycleProfile
from open_energy_planner.appliances.ev.model import EVWork
from open_energy_planner.appliances.thermal.model import ThermalWork
from open_energy_planner.installation.model import InstallationProblem
from open_energy_planner.optimization.domain import Interval
from open_energy_planner.savings.context import physical_context
from open_energy_planner.savings.flexible import reference_context
from open_energy_planner.savings.forecast import comparison
from open_energy_planner.savings.scenarios import scenario, scenarios
from open_energy_planner.savings.windows import window_summary

START = datetime(2026, 11, 19, 8, tzinfo=UTC)


def document(*, cycle=False, grid_limit=10000, prices=(0.4, 0.1)):
    """Freeze cheap-later EV/heat, with an optional movable 0.25-kWh cycle.

    The car needs 0.46 useful kWh. A 2.3-kW AC charger delivers 1.84 kW,
    requiring 0.575 AC kWh. Raising a 0.1-kWh/K tank from 35 to 40 C needs
    0.5 kWh. Both were optimized into the last quarter. The unoptimized
    reference must provide these services in the expensive first quarter.
    """
    end = START + timedelta(minutes=30)
    physics = InstallationProblem(
        tuple(
            Interval(START + timedelta(minutes=15 * index), 0.25, price, 0)
            for index, price in enumerate(prices)
        ),
        (),
        (),
        (),
        grid_limit,
        10000,
        ev_work=(EVWork("session", "point", "car", START, end, 10, 0, 4.6, 0, 2300, "variable"),),
        thermal_work=(ThermalWork("boiler", START, 35, 35, 80, 20, 0.1, 0, 2000, 1, (0, 0), 40),),
        appliances=(ApplianceWork("cycle", "sensor.dishwasher", START, end, CycleProfile((1000,))),)
        if cycle
        else (),
    )
    flows = [
        {
            "grid_import": 0 if index == 0 else 1.075 + (0.25 if cycle else 0),
            "grid_export": 0,
            "batteries": [],
            "inverters": [],
            "solar": [],
            "ev": [
                {
                    "session_id": "session",
                    "ac_kwh": 0 if index == 0 else 0.575,
                    "stored_kwh": 0 if index == 0 else 0.46,
                    "power_w": 0 if index == 0 else 2300,
                }
            ],
            "thermal": [
                {
                    "device_id": "boiler",
                    "ac_kwh": 0 if index == 0 else 0.5,
                    "end_c": 35 if index == 0 else 40,
                    "shortfall_kwh": 0,
                    "unmet_draw_kwh": 0,
                    "power_w": 0 if index == 0 else 2000,
                }
            ],
        }
        for index in range(2)
    ]
    cost = (1.075 + (0.25 if cycle else 0)) * prices[1]
    return json.loads(
        json.dumps(
            {
                "reference_policy": "unoptimized-loads-v1",
                "input_snapshot": {"problem": asdict(physics)},
                "expected_flows": flows,
                "independent_costs": {
                    "cash_cost": cost,
                    "terminal_value": 0,
                    "import_cost": cost,
                    "export_income": 0,
                    "bonus_income": 0,
                    "wear_cost": 0,
                },
                "appliance_schedule": [{"task_id": "cycle", "start": START + timedelta(minutes=15)}]
                if cycle
                else [],
            },
            default=lambda value: value.isoformat(),
        )
    )


@pytest.mark.parametrize("without_battery", [False, True])
@pytest.mark.parametrize("cycle", [False, True])
def test_all_flexible_service_starts_early_and_costs_match_visible_flows(without_battery, cycle):
    """The reference costs 0.43 EUR, or 0.53 with the cycle, versus 0.1075/0.1325 optimized."""
    plan = document(cycle=cycle)
    frozen = deepcopy(plan)
    reference = comparison(plan, without_battery=without_battery)
    expected_energy = 1.325 if cycle else 1.075
    assert [row["import_kwh"] for row in reference["reference"]] == pytest.approx(
        [expected_energy, 0]
    )
    assert reference["reference_costs"]["purchase"] == pytest.approx(0.53 if cycle else 0.43)
    assert reference["expected_savings"] == pytest.approx(0.3975 if cycle else 0.3225)
    chart = scenario(plan, reference, without_battery=without_battery)["chart"]
    assert [flow["ev"][0]["ac_kwh"] for flow in chart["expected_flows"]] == pytest.approx(
        [0.575, 0]
    )
    assert [flow["thermal"][0]["ac_kwh"] for flow in chart["expected_flows"]] == pytest.approx(
        [0.5, 0]
    )
    assert [flow["thermal"][0]["end_c"] for flow in chart["expected_flows"]] == pytest.approx(
        [40, 40]
    )
    assert [step.load_kwh for step in physical_context(chart).intervals] == pytest.approx(
        [expected_energy, 0]
    )
    window = window_summary(
        plan, scenarios(plan, comparison(plan)), START, START + timedelta(minutes=15)
    )
    assert window["scenarios"]["without_battery"]["costs"]["purchase"] == pytest.approx(
        expected_energy * 0.4
    )
    assert window["scenarios"]["optimizer"]["costs"]["purchase"] == 0
    assert plan == frozen


def test_reference_timing_ignores_prices_and_optimizer_timing():
    """Reversing prices and changing valid recorded service timing cannot move the immediate baseline."""
    original = document()
    changed = document(prices=(-0.5, 0.9))
    changed["expected_flows"].reverse()
    first, services = reference_context(original)
    second, other = reference_context(changed)
    assert services == other
    assert [step.load_kwh for step in first.intervals] == [
        step.load_kwh for step in second.intervals
    ]


def test_shared_grid_limit_prioritizes_comfort_then_charges_with_remaining_capacity():
    """The 3-kW connection leaves 1 kW for the EV while the 2-kW heater runs."""
    plan = document(grid_limit=3000)
    physics, services = reference_context(plan)
    assert [flow["ev"][0]["ac_kwh"] for flow in services] == pytest.approx([0.25, 0.325])
    assert [step.load_kwh for step in physics.intervals] == pytest.approx([0.75, 0.325])
    assert comparison(plan, without_battery=True)["financial_issue"] is None


def test_insufficient_reference_service_cannot_appear_as_a_saving():
    """A fixed-power charger cannot fit beside the heater and misses its first-quarter deadline."""
    plan = document(grid_limit=3000)
    work = plan["input_snapshot"]["problem"]["ev_work"][0]
    work.update(control="on_off", deadline=(START + timedelta(minutes=15)).isoformat())
    # Independently feasible original service: charge first, heat second.
    plan["expected_flows"][0]["ev"], plan["expected_flows"][1]["ev"] = (
        plan["expected_flows"][1]["ev"],
        plan["expected_flows"][0]["ev"],
    )
    result = comparison(plan, without_battery=True)
    assert "same charging and comfort service" in result["financial_issue"]
    assert result["reference_costs"] is None
    assert result["expected_savings"] is None


def test_legacy_plan_keeps_original_ev_and_thermal_service_timing():
    """Absence of the versioned new policy reproduces the original fixed-service comparison exactly."""
    plan = document()
    del plan["reference_policy"]
    frozen = deepcopy(plan)
    result = comparison(plan, without_battery=True)
    assert [row["import_kwh"] for row in result["reference"]] == pytest.approx([0, 1.075])
    assert result["expected_savings"] == pytest.approx(0)
    assert "service_flows" not in result
    assert plan == frozen


def test_more_reference_charge_cannot_reward_an_optimizer_that_delivered_less_service():
    """Charging the full 0.46-kWh target is not comparable to a plan delivering only 0.23 kWh."""
    plan = document()
    plan["expected_flows"][1]["ev"][0].update(ac_kwh=0.2875, stored_kwh=0.23, power_w=1150)
    result = comparison(plan, without_battery=True)
    assert result["reference_costs"] is None
    assert result["expected_savings"] is None


def test_more_reference_comfort_cannot_reward_unserved_optimized_heating():
    """A 2-kW quarter would add 5 C, exceeding the 40-C maximum from 39 C.

    A valid fixed-quarter OEP plan must leave a 0.1-kWh comfort/ending deficit.
    A cycling thermostat can add the missing 0.1 kWh. Billing those unequal
    outcomes as a planning saving would credit OEP for not providing comfort.
    """
    plan = document()
    inputs = plan["input_snapshot"]["problem"]
    inputs["ev_work"] = []
    inputs["thermal_work"][0].update(initial_c=39, minimum_c=40, maximum_c=40, terminal_c=40)
    for flow in plan["expected_flows"]:
        flow.update(grid_import=0, ev=[])
        flow["thermal"][0].update(ac_kwh=0, end_c=39, shortfall_kwh=0.1, power_w=0)
    plan["independent_costs"].update(cash_cost=0, import_cost=0)
    result = comparison(plan, without_battery=True)
    assert result["reference_costs"] is None
    assert result["expected_savings"] is None


@pytest.mark.parametrize("difference, comparable", [(1.00021095e-7, True), (0.00001, False)])
def test_numerical_service_roundoff_is_distinct_from_a_real_service_difference(
    difference, comparable
):
    """Observed solver-priority roundoff remains comparable. A ten-micro-kWh service difference does not."""
    plan = document()
    stored = 0.46 - difference
    plan["expected_flows"][1]["ev"][0].update(
        stored_kwh=stored, ac_kwh=stored / 0.8, power_w=stored / 0.8 / 0.25 * 1000
    )
    result = comparison(plan, without_battery=True)
    assert (result["reference_costs"] is not None) is comparable


def test_fixed_running_cycle_and_later_vehicle_availability_are_preserved():
    """A fixed cycle stays at its admitted start and a later-arriving vehicle cannot charge early."""
    plan = document(cycle=True)
    inputs = plan["input_snapshot"]["problem"]
    inputs["appliances"][0]["fixed_start"] = (START + timedelta(minutes=15)).isoformat()
    inputs["ev_work"][0]["ready_at"] = (START + timedelta(minutes=15)).isoformat()
    physics, flows = reference_context(plan)
    assert [flow["ev"][0]["ac_kwh"] for flow in flows] == pytest.approx([0, 0.575])
    assert [step.load_kwh for step in physics.intervals] == pytest.approx([0.5, 0.825])


def test_unknown_reference_policy_is_not_silently_treated_as_a_legacy_plan():
    """An unsupported future policy remains explicitly unavailable instead of inventing its semantics."""
    plan = document()
    plan["reference_policy"] = "unknown-reference-version"
    with pytest.raises(ValueError, match="Unsupported Planning reference policy"):
        comparison(plan)


def test_partial_charge_stops_at_target_without_exceeding_fixed_on_power():
    """Half the requested energy requires a 7.5-minute 2.3-kW pulse, not overcharging a full quarter."""
    plan = document()
    plan["input_snapshot"]["problem"]["ev_work"][0].update(target_soc=2.3, control="on_off")
    _, rows = reference_context(plan)
    assert rows[0]["ev"][0]["stored_kwh"] == pytest.approx(0.23)
    assert rows[0]["ev"][0]["ac_kwh"] == pytest.approx(0.2875)
    assert rows[0]["ev"][0]["power_w"] == pytest.approx(1150)
    assert rows[1]["ev"][0]["ac_kwh"] == 0


def test_thermostat_compensates_draw_and_loss_without_inheriting_optimized_heating():
    """Holding 50 C above 20 C needs 0.06 kWh loss plus 0.1 kWh draw in each quarter."""
    plan = document()
    data = plan["input_snapshot"]["problem"]
    data["ev_work"] = []
    data["thermal_work"][0].update(
        initial_c=50, terminal_c=50, loss_w_per_c=8, demand_kwh=[0.1, 0.1]
    )
    _, rows = reference_context(plan)
    assert [flow["thermal"][0]["ac_kwh"] for flow in rows] == pytest.approx([0.16, 0.16])
    assert [flow["thermal"][0]["end_c"] for flow in rows] == pytest.approx([50, 50])
    assert all(flow["thermal"][0]["shortfall_kwh"] == 0 for flow in rows)


def test_multiple_vehicle_order_is_deadline_then_identity_not_prices_or_input_order():
    """Two cars share one 2.3-kW connection in stable order without claiming simultaneous power."""
    plan = document(grid_limit=2300)
    inputs = plan["input_snapshot"]["problem"]
    inputs["thermal_work"] = []
    first = inputs["ev_work"][0]
    second = {
        **first,
        "session_id": "later-session",
        "point_id": "other-point",
        "vehicle_id": "other-car",
    }
    inputs["ev_work"] = [first, second]
    _, rows = reference_context(plan)
    expected = [[0.575, 0], [0, 0.575]]
    assert [[row["ac_kwh"] for row in step["ev"]] for step in rows] == expected
    inputs["ev_work"].reverse()
    _, reversed_rows = reference_context(plan)
    assert rows == reversed_rows


def test_nonshiftable_weather_sensitive_consumption_keeps_its_original_timeline(tmp_path):
    """A heat pump and air conditioner without cycle permission remain unchanged AC forecast loads."""
    from open_energy_planner.appliances.cycles.service import ApplianceService
    from open_energy_planner.appliances.forecasting import ConsumerForecasts
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

    repository = SqlPlanRepository(tmp_path)
    consumers = ConsumerForecasts(SqlDocuments(repository.engine, "demand"))
    entities = ("sensor.heat_pump", "sensor.air_conditioner")
    consumers.sync(
        {
            "entities": [{"entity_id": entity, "name": entity} for entity in entities],
            "suggestions": {
                "consumers": [{"entity_id": entity, "upstream": None} for entity in entities]
            },
        }
    )
    consumers.save(
        [{"entity_id": entity, "weather_sensitive": True} for entity in entities], consumers.version
    )
    appliance = ApplianceService(SqlDocuments(repository.engine, "planning"), consumers)
    try:
        for entity in entities:
            appliance.configure(entity, False)
            with pytest.raises(ValueError, match="Enable shifting"):
                appliance.submit(entity, "not-authorized", START, START + timedelta(hours=1), START)
        assert not appliance.state["tasks"]
    finally:
        repository.close()
    plan = document()
    inputs = plan["input_snapshot"]["problem"]
    # These loads remain in the household boundary. Weather sensitivity is not permission to shift.
    for step, energy in zip(inputs["intervals"], (0.3, 0.7), strict=True):
        step["load_kwh"] = energy
    frozen = deepcopy(plan)
    problem, flows = reference_context(plan)
    assert [
        step.load_kwh - sum(row["ac_kwh"] for row in (*flow["ev"], *flow["thermal"]))
        for step, flow in zip(problem.intervals, flows, strict=True)
    ] == pytest.approx([0.3, 0.7])
    assert plan == frozen
