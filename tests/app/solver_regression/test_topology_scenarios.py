"""Current physical AC/DC paths and independent converter saturation checks."""

from dataclasses import replace
from fractions import Fraction

import pytest

from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.domain import Status
from tests.app.solver_regression.horizon_cases import grid
from tests.app.solver_regression.topology_cases import TOPOLOGY_CASES, check_topology_plan


@pytest.mark.planner_scenario
@pytest.mark.parametrize("case", TOPOLOGY_CASES, ids=lambda case: case.name)
def test_topology_known_optimum(case, solver):
    """Retain every compatible original converter-loss/rating optimum on the actual installation compiler."""
    result = solver.solve(case.problem)
    assert result.status == Status.OPTIMAL
    check_topology_plan(case, result.plan)
    assert result.best_bound == pytest.approx(float(case.objective), abs=1e-6)


def test_lost_conversion_energy_cannot_be_invented_in_state(solver):
    """Separate .9/.9 conversion stores .81 from the original PV kWh. Forging 1 kWh must fail."""
    case = TOPOLOGY_CASES[0]
    plan = solver.solve(case.problem).plan
    validate(case.problem, plan)
    first = plan.flows[0]
    forged = replace(first, batteries=(replace(first.batteries[0], end_kwh=1),))
    with pytest.raises(ValueError, match="^Stored energy conservation$"):
        validate(case.problem, replace(plan, flows=(forged, plan.flows[1])))


@pytest.mark.planner_scenario
def test_conversion_paths_over_the_confirmed_120_step_horizon(solver):
    """The unchanged separate AC path exports .9 cubed=.729 kWh over the original 48-hour profile."""
    base = TOPOLOGY_CASES[0].problem
    steps = grid(
        lambda elapsed: (
            Fraction(".6"),
            Fraction(".4") if elapsed >= 47 else Fraction(0),
            Fraction(0),
            Fraction(4) if elapsed < Fraction(1, 4) else Fraction(0),
        )
    )
    problem = replace(
        base, intervals=tuple(replace(step, pv_sources=(("pv", step.pv_kwh),)) for step in steps)
    )
    result = solver.solve(problem)
    assert len(problem.intervals) == 120
    assert result.status == Status.OPTIMAL
    assert sum(flow.grid_export for flow in result.plan.flows) == pytest.approx(0.729, abs=1e-6)
    assert validate(problem, result.plan).objective == pytest.approx(-0.729 * 0.4, abs=1e-6)


@pytest.mark.parametrize(
    "invalid", ["missing_inverter", "efficiency", "rating", "shared_without_hybrid"]
)
def test_invalid_topology_is_rejected(invalid):
    """Current configuration rejects missing converter references, invalid efficiency/rating and unmarked shared PV/storage."""
    problem = TOPOLOGY_CASES[0].problem
    expected_issue = {
        "missing_inverter": "Invalid physical inverter links",
        "efficiency": "Invalid solar quantity",
        "rating": "Invalid solar quantity",
        "shared_without_hybrid": "PV and battery sharing requires a hybrid inverter",
    }[invalid]
    with pytest.raises(ValueError, match=f"^{expected_issue}$"):
        if invalid == "missing_inverter":
            replace(problem, batteries=(replace(problem.batteries[0], inverter_id="missing"),))
        elif invalid == "efficiency":
            replace(problem.inverters[0], efficiency=1.1)
        elif invalid == "rating":
            replace(problem.inverters[0], ac_limit_w=-1)
        else:
            replace(problem, batteries=(replace(problem.batteries[0], inverter_id="pv-inverter"),))
