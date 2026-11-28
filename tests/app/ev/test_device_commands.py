"""Current device commands retain original control boundaries across exact billing splits."""

from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.installation.commands import plan_document
from open_energy_planner.installation.model import (
    BatteryFlow,
    InstallationFlow,
    InstallationPlan,
    InverterFlow,
)
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.domain import Costs, SolveResult, Status
from open_energy_planner.synthetic import capture
from tests.app.planning.test_planning_pipeline import NOW


@pytest.mark.parametrize(
    "groups,imports,permission,expected_modes",
    [
        (("quarter", "quarter"), (0, 0.1), True, ["fixed_power"]),
        (("quarter", "quarter"), (0, 0), True, ["follow_grid_target"]),
        (("first", "second"), (0, 0.1), True, ["follow_grid_target", "fixed_power"]),
        (("first", "second"), (0.1, 0.1), False, ["fixed_power", "fixed_power"]),
        ((None, None), (0.1, 0.1), False, ["fixed_power", "fixed_power"]),
    ],
)
def test_billing_splits_preserve_control_boundaries_and_group_following(
    groups, imports, permission, expected_modes
):
    """Two idle half-quarters import exactly their loads. Following needs zero import throughout the original control group."""
    snapshot = capture(NOW, initial_kwh=0.5)
    problem = snapshot.problem
    problem = replace(
        problem,
        batteries=(replace(problem.batteries[0], can_follow_grid=permission),),
        intervals=tuple(
            replace(
                problem.intervals[0],
                start=NOW + timedelta(minutes=7.5 * index),
                duration_h=0.125,
                buy_price=1,
                sell_price=0,
                load_kwh=incoming,
                control_group=group,
            )
            for index, (incoming, group) in enumerate(zip(imports, groups, strict=True))
        ),
    )
    costs = Costs(sum(imports), 0, 0, 0, sum(imports), 0, sum(imports))
    plan = InstallationPlan(
        tuple(
            InstallationFlow(
                incoming,
                0,
                (BatteryFlow("battery", 0, 0, 0.5),),
                (InverterFlow("battery", 0, 0, False),),
                (),
            )
            for incoming in imports
        ),
        costs,
    )
    validate(problem, plan)
    result = SolveResult(Status.FEASIBLE, plan, "independent_fixture", 0, 0, None)
    document = plan_document(replace(snapshot, problem=problem), result, costs, NOW)
    assert [step["devices"]["battery"]["mode"] for step in document["steps"]] == expected_modes
    assert document["steps"][0]["start"] == NOW.isoformat()
    assert document["steps"][-1]["end"] == (NOW + timedelta(minutes=15)).isoformat()
    assert len(document["expected_flows"]) == 2
    assert [flow["grid_import"] for flow in document["expected_flows"]] == list(imports)
    assert document["independent_costs"]["cash_cost"] == sum(imports)


def test_publication_rejects_fractional_battery_power():
    """A forged half-watt battery flow cannot serialize as a rounded executable command."""
    snapshot = capture(NOW, initial_kwh=0.5)
    interval = snapshot.problem.intervals[0]
    problem = replace(snapshot.problem, intervals=(interval,))
    costs = Costs(0, 0, 0, 0, 0, 0, 0)
    discharge_kwh = 0.5 / 1000 * interval.duration_h
    plan = InstallationPlan(
        (
            InstallationFlow(
                0,
                0,
                (BatteryFlow("battery", 0, discharge_kwh, 0.5 - discharge_kwh),),
                (InverterFlow("battery", 0, 0, False),),
                (),
            ),
        ),
        costs,
    )
    result = SolveResult(Status.FEASIBLE, plan, "independent_fixture", 0, 0, None)

    with pytest.raises(ValueError, match="Fractional device command"):
        plan_document(replace(snapshot, problem=problem), result, costs, NOW)
