"""Solve a real two-day teaching plan with hybrid DC and separate AC solar production."""

from dataclasses import replace
from datetime import timedelta

from open_energy_planner.installation.commands import plan_document
from open_energy_planner.installation.model import PVUnit
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import SolveOptions
from open_energy_planner.planning import InputSnapshot
from open_energy_planner.savings.context import physical_context
from open_energy_planner.solar.calculation.model import SolarInverter
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.documentation_fixture import seed as seed_household


def seed(directory):
    """Split each original DC forecast equally between two inverters and independently validate flows."""
    seed_household(directory)
    repository = SqlPlanRepository(directory)
    try:
        original = repository.state["plan"]
        problem = physical_context(original)
        problem = replace(
            problem,
            intervals=tuple(
                replace(step, pv_sources=(("roof", step.pv_kwh / 2), ("garage", step.pv_kwh / 2)))
                for step in problem.intervals
            ),
            inverters=problem.inverters + (SolarInverter("solar-ac", 3000, 0.9),),
            pv_sources=problem.pv_sources + (PVUnit("garage", "solar-ac", True),),
            batteries=tuple(replace(unit, dc_efficiency=0.9) for unit in problem.batteries),
        )
        snapshot = InputSnapshot(
            "mixed-solar-example",
            "example-installation",
            START,
            "example-meters",
            "example-forecasts",
            "example-prices",
            problem,
            contract_version=original["input_snapshot"]["contract_version"],
            tariff_evidence=tuple(original["input_snapshot"]["tariff_evidence"]),
        )
        solution = HighsSolver().solve(problem, SolveOptions(30))
        assert solution.plan is not None, (solution.status, solution.stop_reason)
        costs = validate(problem, solution.plan)
        document = plan_document(snapshot, solution, costs, START + timedelta(seconds=1))
        document.update(
            currency="EUR",
            market_area="NL",
            comparison_names={**original["comparison_names"], "garage": "Example AC roof"},
        )
        repository.state["plan"] = document
        repository.save()
    finally:
        repository.close()
