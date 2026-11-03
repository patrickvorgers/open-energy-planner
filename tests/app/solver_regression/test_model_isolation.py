"""Current component binding ownership and independent model-state behavior."""

from dataclasses import replace

import highspy
import pytest

from open_energy_planner.installation.model import BatteryState, PVUnit
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs.context import HighsModelContext
from open_energy_planner.optimization.adapters.highs.installation import InstallationModelFactory
from open_energy_planner.optimization.adapters.highs.installation_components import (
    BusBattery,
    BusInverter,
    BusPV,
)
from open_energy_planner.optimization.domain import Battery
from open_energy_planner.solar.calculation.model import SolarInverter
from tests.app.solver_regression.cases import CORE, intervals


def run(context):
    """Solve pinned current component inputs and require actual native optimal termination."""
    context.native.setOptionValue("threads", 1)
    assert context.native.run() == highspy.HighsStatus.kOk
    assert context.native.getModelStatus() == highspy.HighsModelStatus.kOptimal


def pin(context, binding, name, values):
    """Pin real component variables to independent energy inputs without inventing route provenance."""
    for variable, value in zip(binding.variables[name], values, strict=True):
        context.native.changeColBounds(int(variable), value, value)


def battery_component(context, device_id, battery, charge):
    """Declare current battery/inverter bindings and prescribe terminal energy for state isolation."""
    unit = BatteryState(device_id, device_id, battery)
    converter = BusInverter(SolarInverter(device_id, 4000, 1), (unit,), ())
    converter_binding = converter.declare_variables(context)
    pin(context, converter_binding, "clip", (0,) * len(context.intervals))
    component = BusBattery(unit)
    binding = component.declare_variables(context)
    component.add_constraints(context, binding)
    pin(context, binding, "charge", charge)
    pin(context, binding, "discharge", (0,) * len(context.intervals))
    return binding


def test_multiple_battery_instances_have_independent_states_and_power_limits():
    """Original .5*.9=.45 and .2+.25*.8=.4 states stay isolated when A changes and C is added."""
    context = HighsModelContext(intervals((0.1,), (0,)))
    first = battery_component(context, "a", Battery(1, 0, charge_efficiency=0.9), (0.5,))
    second = battery_component(
        context, "b", Battery(2, 0.2, charge_efficiency=0.8, charge_limit_w=1000), (0.25,)
    )
    run(context)
    assert context.native.val(first.variables["energy"][0]) == pytest.approx(0.45)
    assert context.native.val(second.variables["energy"][0]) == pytest.approx(0.4)
    third = battery_component(context, "c", Battery(1, 0), (0.1,))
    pin(context, first, "charge", (0.1,))
    run(context)
    assert context.native.val(first.variables["energy"][0]) == pytest.approx(0.09)
    assert context.native.val(second.variables["energy"][0]) == pytest.approx(0.4)
    assert context.native.val(third.variables["energy"][0]) == pytest.approx(0.1)


def test_one_battery_definition_can_build_two_independent_scenarios():
    """The same current battery definition stores .2/.4 under .25/.5 inputs in independent native contexts."""
    component = BusBattery(
        BatteryState("battery", "converter", Battery(1, 0, charge_efficiency=0.8))
    )
    contexts = [HighsModelContext(intervals((0.1,), (0,))) for _ in range(2)]
    bindings = []
    for context, charge in zip(contexts, (0.25, 0.5), strict=True):
        converter = BusInverter(SolarInverter("converter", 4000, 1), (component.unit,), ())
        binding = converter.declare_variables(context)
        pin(context, binding, "clip", (0,))
        binding = component.declare_variables(context)
        component.add_constraints(context, binding)
        pin(context, binding, "charge", (charge,))
        pin(context, binding, "discharge", (0,))
        run(context)
        bindings.append(binding)
    assert contexts[0].native.val(bindings[0].variables["energy"][0]) == pytest.approx(0.2)
    assert contexts[1].native.val(bindings[1].variables["energy"][0]) == pytest.approx(0.4)
    contexts[0].native.clear()
    run(contexts[1])
    assert contexts[1].native.val(bindings[1].variables["energy"][0]) == pytest.approx(0.4)


def test_two_solar_instances_do_not_share_curtailment_or_switch_state():
    """Independent 1-kWh sources remain fully on for fixed A and fully off for controllable B."""
    steps = (replace(intervals((0.2,), (-0.05,))[0], pv_kwh=2, pv_sources=(("a", 1), ("b", 1))),)
    context = HighsModelContext(steps)
    for identity, permission in (("a", False), ("b", True)):
        source = PVUnit(identity, identity, permission)
        converter = BusInverter(SolarInverter(identity, 4000, 1), (), (source,))
        converter_binding = converter.declare_variables(context)
        pin(context, converter_binding, "clip", (0,))
        component = BusPV(source)
        binding = component.declare_variables(context)
        component.add_constraints(context, binding)
        if permission:
            pin(context, binding, "on", (0,))
    run(context)
    assert context.native.val(context.binding("pv:a").variables["used"][0]) == 1
    assert context.native.val(context.binding("pv:b").variables["used"][0]) == 0
    assert context.native.val(context.binding("pv:b").variables["on"][0]) == 0


def test_factory_reference_trace_and_optimized_model_keep_independent_energy():
    """Idle reference keeps its original .5 kWh while a separate optimization attains S01's -.28."""
    factory = InstallationModelFactory()
    case = CORE[0]
    optimized = factory.build(case.problem)
    units = (
        replace(
            case.problem.batteries[0],
            battery=replace(case.problem.batteries[0].battery, initial_kwh=0.5),
        ),
    )
    reference_problem = replace(case.problem, batteries=units)
    reference = factory.build(reference_problem)
    binding = reference.context.binding("bat:battery")
    for name in ("charge", "discharge"):
        pin(reference.context, binding, name, (0, 0))
    run(optimized.context)
    run(reference.context)
    plan = optimized.extract()
    assert validate(case.problem, plan).objective == pytest.approx(float(case.objective))
    reference_plan = reference.extract()
    assert validate(reference_problem, reference_plan).objective == 0
    assert [flow.batteries[0].end_kwh for flow in reference_plan.flows] == [0.5, 0.5]
    optimized.context.native.clear()
    run(reference.context)
    assert reference.extract() == reference_plan
    assert plan.flows[0].batteries[0].end_kwh == pytest.approx(1)


@pytest.mark.parametrize("failure", ["duplicate", "missing"])
def test_invalid_component_composition_is_rejected(failure):
    """Current model bindings reject duplicate identities and missing references before constraint creation."""
    context = HighsModelContext(intervals((0.1,), (0,)))
    expected_issue = {
        "duplicate": "Empty or duplicate device ID: battery",
        "missing": "Unresolved device/connection: missing",
    }[failure]
    with pytest.raises(ValueError, match=f"^{expected_issue}$"):
        if failure == "duplicate":
            context.register("battery")
            context.register("battery")
        else:
            context.binding("missing")


def test_foreign_binding_cannot_add_constraints_to_another_scenario():
    """A binding from another native model cannot add rows in the receiving context."""
    first = HighsModelContext(intervals((0.1,), (0,)))
    second = HighsModelContext(first.intervals)
    component = BusBattery(BatteryState("battery", "converter", Battery(1, 0)))
    foreign = component.declare_variables(first)
    component.declare_variables(second)
    before = second.native.getNumRow()
    with pytest.raises(ValueError, match="different model/scenario"):
        component.add_constraints(second, foreign)
    assert second.native.getNumRow() == before


def test_component_cannot_apply_its_parameters_to_another_device_binding():
    """A battery's parameters cannot be attached to another device's valid local binding."""
    context = HighsModelContext(intervals((0.1,), (0,)))
    first = battery_component(context, "a", Battery(1, 0), (0.1,))
    component = BusBattery(BatteryState("b", "unused-converter", Battery(2, 0)))
    before = context.native.getNumRow()
    with pytest.raises(ValueError, match="different device"):
        component.add_constraints(context, first)
    assert context.native.getNumRow() == before
