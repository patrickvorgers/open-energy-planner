"""A real two-day plan whose ready EV can charge now or in the cheaper night."""

from dataclasses import replace
from datetime import timedelta

from open_energy_planner.appliances.ev.model import EVWork, refine
from open_energy_planner.appliances.thermal.model import ThermalWork
from open_energy_planner.appliances.thermal.service import refine as refine_heat
from open_energy_planner.demand.calculation.composition import LoadComponent, LoadStep
from open_energy_planner.installation.commands import plan_document
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import SolveOptions
from open_energy_planner.planning import InputSnapshot
from open_energy_planner.savings.context import physical_context
from open_energy_planner.savings.forecast import comparison
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.documentation_fixture import seed as seed_household


def seed(directory):
    """Preserve full solar/household variation and independently validate the solved flexible plan."""
    seed_household(directory)
    repository = SqlPlanRepository(directory)
    try:
        original = repository.state["plan"]
        problem = refine_heat(physical_context(original), START)
        vehicle = EVWork(
            "example-session",
            "example-point",
            "example-car",
            START + timedelta(hours=18),
            START + timedelta(hours=32),
            60,
            40,
            50,
            2300,
            7000,
            "variable",
        )
        problem = refine(problem, (vehicle,))
        problem = replace(
            problem,
            thermal_work=(
                ThermalWork(
                    "example-boiler",
                    START,
                    50,
                    45,
                    70,
                    20,
                    0.1,
                    0,
                    2000,
                    1,
                    tuple(
                        2 * step.duration_h if step.start.hour == 19 else 0
                        for step in problem.intervals
                    ),
                    50,
                ),
            ),
        )
        snapshot = InputSnapshot(
            "flexible-reference-example",
            "example-installation",
            START,
            "example-meters",
            "example-forecasts",
            "example-prices",
            problem,
            contract_version=original["input_snapshot"]["contract_version"],
            load_components=(
                LoadComponent(
                    None,
                    "example-household",
                    tuple(
                        LoadStep(step.start, step.duration_h, step.load_kwh)
                        for step in problem.intervals
                    ),
                    tuple(
                        LoadStep(step.start, step.duration_h, step.load_kwh)
                        for step in problem.intervals
                    ),
                ),
                *(
                    LoadComponent(
                        entity,
                        "example-device",
                        tuple(
                            LoadStep(step.start, step.duration_h, 0) for step in problem.intervals
                        ),
                        tuple(
                            LoadStep(step.start, step.duration_h, 0) for step in problem.intervals
                        ),
                    )
                    for entity in ("sensor.example_car_energy", "sensor.example_boiler_energy")
                ),
            ),
            ev_evidence=(
                {
                    "work": {
                        "point_id": "example-point",
                        "vehicle_id": "example-car",
                        "session_id": "example-session",
                    },
                    "energy_entity": "sensor.example_car_energy",
                },
            ),
            thermal_evidence=(
                {
                    "configuration": {
                        "id": "example-boiler",
                        "energy_entity": "sensor.example_boiler_energy",
                    }
                },
            ),
        )
        result = HighsSolver().solve(problem, SolveOptions(30))
        assert result.plan is not None, result.status
        costs = validate(problem, result.plan)
        document = plan_document(snapshot, result, costs, START + timedelta(seconds=1))
        document.update(
            reference_policy="unoptimized-loads-v1",
            currency="EUR",
            market_area="NL",
            comparison_names={
                **original["comparison_names"],
                "example-point": "Example car",
                "example-car": "Example car",
                "sensor.example_car_energy": "Example car",
                "example-boiler": "Example boiler",
                "sensor.example_boiler_energy": "Example boiler",
            },
        )
        document["reference_forecast"] = comparison(document)
        repository.state["plan"] = document
        repository.save()
    finally:
        repository.close()
