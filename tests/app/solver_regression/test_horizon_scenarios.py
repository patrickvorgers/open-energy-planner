"""Mixed-resolution horizon plus independent known optima and refinement checks."""

from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.optimization.domain import Status
from tests.app.solver_regression.horizon_cases import (
    HORIZON_CASES,
    START,
    check_horizon_plan,
    quarter_hour_problem,
)


def test_confirmed_time_grid_is_48_hours_with_120_decisions():
    """Inspect every horizon fixture. Require 96 quarter-hours plus 24 hours with contiguous 48-hour coverage."""
    for case in HORIZON_CASES:
        intervals = case.problem.intervals
        assert len(intervals) == 120
        assert [interval.duration_h for interval in intervals] == [0.25] * 96 + [1] * 24
        assert intervals[96].start == START + timedelta(hours=24)
        assert intervals[-1].start + timedelta(hours=1) == START + timedelta(hours=48)
        assert sum(interval.duration_h for interval in intervals) == 48


@pytest.mark.planner_scenario
@pytest.mark.parametrize("case", HORIZON_CASES, ids=lambda case_item: case_item.name)
def test_horizon_known_optimum(case, solver):
    """Solve each 48-hour case and attain HorizonCase's independent energy/cost bounds.

    Direct demand arithmetic, round-trip losses, remaining bonus volume and terminal
    stock valuation determine the expected result independently of the solver.
    """
    result = solver.solve(case.problem)
    assert result.status == Status.OPTIMAL
    assert result.plan is not None
    check_horizon_plan(case, result.plan)
    assert result.best_bound == pytest.approx(float(case.objective), abs=1e-6, rel=0)


@pytest.mark.planner_scenario
@pytest.mark.parametrize("case", HORIZON_CASES, ids=lambda case_item: case_item.name)
def test_hourly_tail_preserves_known_economics_when_profile_is_constant_inside_hour(case, solver):
    """Refine hourly tail slots to quarters. Require unchanged economics because their profiles are constant."""
    refined = replace(case, problem=quarter_hour_problem(case.problem))
    assert len(refined.problem.intervals) == 192
    result = solver.solve(refined.problem)
    assert result.status == Status.OPTIMAL
    assert result.plan is not None
    check_horizon_plan(refined, result.plan)
    assert result.best_bound == pytest.approx(float(case.objective), abs=1e-6, rel=0)
