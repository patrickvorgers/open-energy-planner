"""Deliberately corrupt current per-device candidates to exercise independent validation."""

from dataclasses import replace

import pytest

from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.domain import Costs, Status
from tests.app.solver_regression.cases import CORE, installation, intervals


@pytest.fixture
def plans(solver):
    """Solve only retained original cases before mutating actual current result contracts."""
    candidates = []
    for case in CORE:
        result = solver.solve(case.problem)
        assert result.status == Status.OPTIMAL
        validate(case.problem, result.plan)
        candidates.append(result.plan)
    return candidates


@pytest.mark.parametrize(
    ("mutation", "expected_issue"),
    [
        ("grid_limit", "Grid bound"),
        ("state", "Stored energy conservation"),
        ("missing_battery", "Battery identities"),
        ("nonfinite", "Grid bound"),
        ("negative_battery", "Finite battery energy"),
    ],
)
def test_corrupted_route_or_state_is_rejected(plans, mutation, expected_issue):
    """Reject forged grid exchange or battery energy using current physical records instead of retired routes."""
    plan = plans[0]
    flow = plan.flows[0]
    if mutation == "grid_limit":
        flow = replace(flow, grid_import=1.1)
    elif mutation == "missing_battery":
        flow = replace(flow, batteries=())
    elif mutation == "nonfinite":
        flow = replace(flow, grid_import=float("nan"))
    else:
        flow = replace(
            flow,
            batteries=(replace(flow.batteries[0], end_kwh=0.9 if mutation == "state" else -0.1),),
        )
    with pytest.raises(ValueError, match=expected_issue):
        validate(CORE[0].problem, replace(plan, flows=(flow, *plan.flows[1:])))


@pytest.mark.parametrize(
    "component", ["import_cost", "bonus_income", "wear_cost", "terminal_value", "objective"]
)
def test_corrupted_financial_component_is_rejected(plans, component):
    """A .01 forged financial component fails recomputation from authoritative current flows."""
    plan = plans[0]
    costs = replace(plan.costs, **{component: getattr(plan.costs, component) + 0.01})
    with pytest.raises(ValueError, match="Financial totals"):
        validate(CORE[0].problem, replace(plan, costs=costs))


def test_missing_interval_is_rejected(plans):
    """Removing one solved interval cannot become a complete publishable installation plan."""
    with pytest.raises(ValueError, match="Installation plan shape"):
        validate(CORE[0].problem, replace(plans[0], flows=plans[0].flows[:1]))


def test_partial_production_cannot_masquerade_as_on_off_control(solver):
    """Reject .6 kWh of undeclared clipping from an enabled source with 1 kWh available.

    With zero load and a sale rate of .1, the forged .4 kWh production/export
    earns .4 * .1 = .04. Matching cash/objective costs of -.04 isolate the PV
    control violation from AC conservation and financial-total checks.
    """
    problem = installation(intervals((0.2,), (0.1,), pv=1), can_curtail=True)
    plan = solver.solve(problem).plan
    validate(problem, plan)
    flow = plan.flows[0]
    forged = replace(
        flow,
        grid_export=0.4,
        solar=(replace(flow.solar[0], used_kwh=0.4),),
        inverters=(replace(flow.inverters[0], export_kwh=0.4),),
    )
    costs = Costs(0, 0.04, 0, 0, -0.04, 0, -0.04)
    with pytest.raises(ValueError, match="Undeclared PV clipping"):
        validate(problem, replace(plan, flows=(forged,), costs=costs))
