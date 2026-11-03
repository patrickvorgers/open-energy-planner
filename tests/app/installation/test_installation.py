"""Independent multi-inverter scenarios for installation compilation, physics and commands."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.installation.model import BatteryState, InstallationProblem, PVUnit
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import Battery, Interval, Status
from open_energy_planner.optimization.export_bonus import ExportBonus
from open_energy_planner.solar.calculation.model import SolarInverter
from tests.database.support.archive import corrupt_document, list_documents, make_store

START = datetime(2026, 6, 20, 0, tzinfo=UTC)


def problem(
    *,
    prices=(1, 3),
    loads=(0, 1),
    pv=(0, 0),
    efficiency=1,
    initial=0,
    hybrid=True,
    curtail=False,
    grid=4000,
):
    """Declare a 1 kWh battery with ideal cell efficiency and an independently specified converter."""
    return InstallationProblem(
        tuple(
            Interval(
                START + timedelta(hours=interval_index),
                1,
                price,
                0,
                load,
                production,
                pv_sources=(("pv", production),),
            )
            for interval_index, (price, load, production) in enumerate(
                zip(prices, loads, pv, strict=True)
            )
        ),
        (SolarInverter("inverter", 1000, efficiency, hybrid),),
        (
            BatteryState(
                "battery",
                "inverter",
                Battery(1, initial, charge_limit_w=1000, discharge_limit_w=1000),
            ),
        ),
        (PVUnit("pv", "inverter", curtail),),
        grid,
        4000,
    )


def solve(inputs):
    """Run the production HiGHS lifecycle and always independently check a returned usable candidate."""
    result = HighsSolver().solve(inputs)
    if result.plan:
        validate(inputs, result.plan)
    return result


@pytest.mark.planner_scenario
def test_installation_charges_cheap_and_discharges_expensive_without_aggregating_state():
    """Buying 1 kWh at 1 and using it against price 3 costs exactly 1, with whole -1000/+1000 W commands."""
    inputs = problem()
    result = solve(inputs)
    assert result.status == Status.OPTIMAL
    assert result.plan.costs.cash_cost == pytest.approx(1)
    assert result.plan.flows[0].batteries[0].charge_kwh == pytest.approx(1)
    assert result.plan.flows[1].batteries[0].discharge_kwh == pytest.approx(1)


@pytest.mark.planner_scenario
def test_mixed_standalone_pv_and_hybrid_direct_dc_avoid_double_conversion():
    """Standalone PV supplies 0.8 kWh now. 1 kWh direct DC fills storage and supplies 0.8 AC later with no grid."""
    inputs = problem(loads=(0.8, 0.8), pv=(1, 0), efficiency=0.8, grid=0)
    inputs = replace(
        inputs,
        inverters=(*inputs.inverters, SolarInverter("standalone", 1000, 1)),
        pv_sources=(*inputs.pv_sources, PVUnit("roof2", "standalone", False)),
        intervals=tuple(
            replace(
                interval,
                pv_sources=(*interval.pv_sources, ("roof2", 0.8 if interval_index == 0 else 0)),
            )
            for interval_index, interval in enumerate(inputs.intervals)
        ),
    )
    result = solve(inputs)
    assert result.status == Status.OPTIMAL
    assert result.plan.flows[0].batteries[0].end_kwh == pytest.approx(1)
    assert result.plan.flows[1].batteries[0].end_kwh == pytest.approx(0)
    assert sum(flow_item.grid_import for flow_item in result.plan.flows) == pytest.approx(0)
    assert result.plan.flows[0].inverters[0].export_kwh == pytest.approx(0)


@pytest.mark.planner_scenario
def test_multiple_batteries_keep_independent_terminal_limits_and_charge_states():
    """Two independently full 1 kWh batteries each supply 1 kWh. Neither can stand in for the other's measured state."""
    inputs = problem(prices=(3,), loads=(2,), pv=(0,), initial=1, grid=0)
    inputs = replace(
        inputs,
        inverters=(*inputs.inverters, SolarInverter("other", 1000, 1)),
        batteries=(
            *inputs.batteries,
            BatteryState(
                "other_battery", "other", Battery(1, 1, charge_limit_w=1000, discharge_limit_w=1000)
            ),
        ),
    )
    result = solve(inputs)
    assert result.status == Status.OPTIMAL
    assert {
        battery.device_id: battery.discharge_kwh for battery in result.plan.flows[0].batteries
    } == pytest.approx({"battery": 1, "other_battery": 1})


@pytest.mark.parametrize("curtail,expected", [(False, 1), (True, -1)])
@pytest.mark.planner_scenario
def test_negative_price_cannot_switch_off_fixed_pv_but_permitted_shutdown_can_earn_import_credit(
    curtail, expected
):
    """Fixed 2 kWh DC fills 1 kWh storage and exports 1 at -1. Permitted shutdown instead imports 1 at -1."""
    inputs = problem(prices=(-1,), loads=(0,), pv=(2,), curtail=curtail)
    inputs = replace(inputs, intervals=(replace(inputs.intervals[0], sell_price=-1),))
    result = solve(inputs)
    assert result.status == Status.OPTIMAL
    assert result.plan.costs.cash_cost == pytest.approx(expected)
    assert result.plan.flows[0].solar[0].enabled is not curtail


@pytest.mark.planner_scenario
def test_export_bonus_credits_only_first_half_kwh_of_one_kwh_export():
    """One fixed exported kWh at zero base price earns exactly 0.5 from rate 1 and a 0.5 kWh remaining cap."""
    inputs = problem(prices=(0,), loads=(0,), pv=(1,))
    inputs = replace(
        inputs, batteries=(), export_bonuses=(ExportBonus("bonus", (1,), (0.5,), ("period",), 0),)
    )
    result = solve(inputs)
    assert result.status == Status.OPTIMAL
    assert result.plan.flows[0].grid_export == pytest.approx(1)
    assert result.plan.costs.bonus_income == pytest.approx(0.5)


def test_independent_validator_rejects_converter_energy_created_after_solve():
    """Changing converter export by 0.1 kWh breaks bus/DC conservation even if the reported solver status was optimal."""
    inputs = problem()
    plan = solve(inputs).plan
    flow = plan.flows[1]
    altered = replace(
        flow, inverters=(replace(flow.inverters[0], export_kwh=flow.inverters[0].export_kwh + 0.1),)
    )
    with pytest.raises(ValueError, match="conservation|bounds"):
        validate(inputs, replace(plan, flows=(plan.flows[0], altered)))


@pytest.mark.parametrize("initial,expected_charge", [(0, 1), (0.9, 0.1), (1, 0)])
@pytest.mark.planner_scenario
def test_natural_clipping_requires_saturated_converter_and_storage_acceptance(
    initial, expected_charge
):
    """With 2 kWh fixed PV and a 1 kW converter, storage fills its remaining room before legitimate AC clipping."""
    inputs = problem(prices=(0,), loads=(0,), pv=(2,), initial=initial)
    inputs = replace(inputs, intervals=(replace(inputs.intervals[0], sell_price=-1),))
    result = solve(inputs)
    assert result.status == Status.OPTIMAL
    flow = result.plan.flows[0]
    assert flow.batteries[0].charge_kwh == pytest.approx(expected_charge)
    assert flow.grid_export == pytest.approx(1)
    assert flow.solar[0].used_kwh == pytest.approx(1 + expected_charge)


@pytest.mark.planner_scenario
def test_total_connection_limit_makes_unserved_fixed_demand_infeasible():
    """A 1 kW connection cannot supply 2 kWh in an hour without PV/storage. No missing energy is fabricated."""
    inputs = replace(problem(prices=(1,), loads=(2,), pv=(0,), grid=1000), batteries=())
    assert solve(inputs).status == Status.INFEASIBLE


@pytest.mark.planner_scenario
def test_separate_conversion_cannot_claim_hybrid_dc_efficiency():
    """1 kWh PV crosses two 80% converters before storage and one on discharge, so it cannot supply 0.8 AC later without grid."""
    inputs = problem(loads=(0, 0.8), pv=(1, 0), efficiency=0.8, grid=0)
    inputs = replace(
        inputs,
        inverters=(*inputs.inverters, SolarInverter("separate", 1000, 0.8)),
        batteries=(replace(inputs.batteries[0], inverter_id="separate"),),
    )
    assert solve(inputs).status == Status.INFEASIBLE


def test_component_binding_cannot_leak_between_scenario_contexts():
    """Reusing a stateless battery definition is allowed, but a native binding from another model must be rejected."""
    from open_energy_planner.optimization.adapters.highs.context import HighsModelContext
    from open_energy_planner.optimization.adapters.highs.installation_components import BusBattery

    inputs = problem()
    component = BusBattery(inputs.batteries[0])
    first, second = HighsModelContext(inputs.intervals), HighsModelContext(inputs.intervals)
    binding = component.declare_variables(first)
    component.declare_variables(second)
    with pytest.raises(ValueError, match="different model"):
        component.add_constraints(second, binding)


def test_installation_store_preserves_versions_and_rejects_corruption(tmp_path):
    """Settings share an immutable hash across integer/float input and never turn a corrupt active reference into an empty installation."""
    from open_energy_planner.installation.storage import InstallationStore, parse

    config = parse(
        {"grid_power_entity": "sensor.grid", "import_limit_w": 5000, "export_limit_w": 5000}
    )
    store = make_store(tmp_path, InstallationStore)
    store.save(config)
    assert make_store(tmp_path, InstallationStore).current == config
    assert config.version == replace(config, import_limit_w=5000.0).version
    store.save(replace(config, import_limit_w=4000))
    assert len(list_documents(tmp_path, "installation")) == 3
    corrupt_document(tmp_path, "installation", "current.json", '"../../invalid"')
    assert make_store(tmp_path, InstallationStore).issue == "installation_configuration_unreadable"


@pytest.mark.planner_scenario
def test_optional_dc_converter_loss_is_applied_at_battery_terminals_once_per_direction():
    """1 kWh PV through 80% DC conversion stores 0.8. Discharge through 80% DC and 80% AC yields exactly 0.512 kWh."""
    inputs = problem(loads=(0, 0.512), pv=(1, 0), efficiency=0.8, grid=0)
    inputs = replace(inputs, batteries=(replace(inputs.batteries[0], dc_efficiency=0.8),))
    result = solve(inputs)
    assert result.status == Status.OPTIMAL
    assert result.plan.flows[0].batteries[0].end_kwh == pytest.approx(0.8)
    assert result.plan.flows[1].inverters[0].export_kwh == pytest.approx(0.512)


@pytest.mark.planner_scenario
def test_billing_segments_preserve_equal_terminal_power_with_distinct_prices():
    """A shared control hour buys two halves at -1/+1, costing zero, to supply 1 kWh at price 3 later. Both halves must charge at 1000 W."""
    inputs = problem(prices=(-1, 1, 3), loads=(0, 0, 1), pv=(0, 0, 0))
    inputs = replace(
        inputs,
        intervals=(
            replace(inputs.intervals[0], duration_h=0.5, control_group="hour"),
            replace(
                inputs.intervals[1],
                start=START + timedelta(minutes=30),
                duration_h=0.5,
                control_group="hour",
            ),
            replace(inputs.intervals[2], start=START + timedelta(hours=1)),
        ),
    )
    result = solve(inputs)
    assert result.status == Status.OPTIMAL
    assert result.plan.costs.cash_cost == pytest.approx(0)
    assert [
        flow_item.batteries[0].charge_kwh for flow_item in result.plan.flows[:2]
    ] == pytest.approx([0.5, 0.5])


@pytest.mark.planner_scenario
def test_two_hybrid_inverters_keep_direct_dc_links_local_to_their_own_batteries():
    """Two separate 1 kWh DC sources fill their own batteries. Together they deliver 1.6 AC after 80% conversion, without lossy cross-routing."""
    inputs = problem(loads=(0, 1.6), pv=(1, 0), efficiency=0.8, grid=0)
    inputs = replace(
        inputs,
        inverters=(*inputs.inverters, SolarInverter("hybrid2", 1000, 0.8, True)),
        batteries=(
            *inputs.batteries,
            BatteryState(
                "battery2", "hybrid2", Battery(1, 0, charge_limit_w=1000, discharge_limit_w=1000)
            ),
        ),
        pv_sources=(*inputs.pv_sources, PVUnit("pv2", "hybrid2", False)),
        intervals=tuple(
            replace(
                interval,
                pv_sources=(*interval.pv_sources, ("pv2", 1 if interval_index == 0 else 0)),
            )
            for interval_index, interval in enumerate(inputs.intervals)
        ),
    )
    result = solve(inputs)
    assert result.status == Status.OPTIMAL
    assert [battery.end_kwh for battery in result.plan.flows[0].batteries] == pytest.approx([1, 1])
    assert sum(flow_item.grid_import for flow_item in result.plan.flows) == pytest.approx(0)


@pytest.mark.planner_scenario
def test_multiple_charging_batteries_share_one_import_limit():
    """Two 1 kW chargers behind a 1 kW connection can jointly buy only 1 kWh in an hour, earning exactly 1 at price -1."""
    inputs = problem(prices=(-1,), loads=(0,), pv=(0,), grid=1000)
    inputs = replace(
        inputs,
        inverters=(*inputs.inverters, SolarInverter("other", 1000, 1)),
        batteries=(
            *inputs.batteries,
            BatteryState(
                "other_battery", "other", Battery(1, 0, charge_limit_w=1000, discharge_limit_w=1000)
            ),
        ),
    )
    result = solve(inputs)
    assert result.status == Status.OPTIMAL
    assert sum(battery.charge_kwh for battery in result.plan.flows[0].batteries) == pytest.approx(1)
    assert result.plan.costs.cash_cost == pytest.approx(-1)


def test_indexed_projection_preserves_partial_interval_energy():
    """Three quarters of a 1 kWh hour plus half of a 2 kWh hour integrate to 1.75 kWh, without scanning unrelated rows."""
    from open_energy_planner.installation.basis import EnergySeries
    from open_energy_planner.solar.calculation.model import SolarEnergy

    rows = (
        SolarEnergy("pv", START, 1, 1, 1),
        SolarEnergy("pv", START + timedelta(hours=1), 1, 2, 2),
    )
    series = EnergySeries(rows, "dc_kwh")
    assert series.integral(
        START + timedelta(minutes=15), START + timedelta(minutes=90)
    ) == pytest.approx(1.75)


def test_indexed_projection_rejects_overlapping_evidence_and_internal_gaps():
    """Duplicate/overlapping forecast rows cannot double-count production, and an interior missing hour cannot become zero."""
    from open_energy_planner.home_assistant.observations import InputsUnavailable
    from open_energy_planner.installation.basis import EnergySeries
    from open_energy_planner.solar.calculation.model import SolarEnergy

    first = SolarEnergy("pv", START, 1, 1, 1)
    with pytest.raises(InputsUnavailable, match="coverage_missing"):
        EnergySeries((first, first), "dc_kwh")
    series = EnergySeries((first, replace(first, start=START + timedelta(hours=2))), "dc_kwh")
    with pytest.raises(InputsUnavailable, match="coverage_missing"):
        series.integral(START, START + timedelta(hours=3))


def test_installation_problem_and_plan_cross_the_bounded_worker_process():
    """The real spawned solver process accepts typed installation input and returns a plain independently valid plan costing exactly 1."""
    import asyncio

    from open_energy_planner.optimization.adapters.process.worker import ProcessSolver

    inputs = problem()
    result = asyncio.run(ProcessSolver().solve(inputs))
    assert result.status == Status.OPTIMAL
    assert validate(inputs, result.plan).cash_cost == pytest.approx(1)


@pytest.mark.planner_scenario
@pytest.mark.parametrize("export_limit", [0, 500])
def test_fixed_pv_does_not_gain_export_limiting_permission(export_limit):
    """One full battery, 2 kWh fixed PV and no load cannot fit a 0/0.5 kWh export cap. Grid saturation must not invent PV control."""
    from types import SimpleNamespace

    from open_energy_planner.installation.api import warnings
    from open_energy_planner.solar.calculation.model import SolarSource

    inputs = replace(
        problem(prices=(0,), loads=(0,), pv=(2,), initial=1),
        grid_export_limit_w=export_limit,
    )
    assert solve(inputs).status == Status.INFEASIBLE
    store = SimpleNamespace(current=SimpleNamespace(export_limit_w=export_limit))
    source = SolarSource("pv", "sensor.pv", "inverter", 2000, "south")
    solar = SimpleNamespace(
        configuration=SimpleNamespace(sources=(source,), inverters=inputs.inverters)
    )
    assert warnings(store, solar) == ["fixed_pv_export_limit"]
    solar.configuration.sources = (replace(source, can_curtail=True),)
    assert warnings(store, solar) == []
    permitted = replace(inputs, pv_sources=(PVUnit("pv", "inverter", True),))
    assert solve(permitted).status == Status.OPTIMAL
