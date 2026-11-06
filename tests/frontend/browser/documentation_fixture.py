"""Provide synthetic teaching data with real validated plans and visible daily variation."""

from datetime import UTC, datetime, timedelta
from math import pi, sin

from open_energy_planner.contracts.calculation.formula import Formula
from open_energy_planner.contracts.model import ContractPeriod, ContractSchedule, TariffEvidence
from open_energy_planner.contracts.storage import ContractStore
from open_energy_planner.installation.commands import plan_document
from open_energy_planner.installation.model import BatteryState, InstallationProblem, PVUnit
from open_energy_planner.installation.validation import validate
from open_energy_planner.installation.valuation import TerminalValuation
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import Battery, Interval, SolveOptions
from open_energy_planner.planning import InputSnapshot
from open_energy_planner.planning_policy import planning_grid
from open_energy_planner.solar.calculation.model import SolarInverter
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

START = datetime(2026, 11, 4, tzinfo=UTC)


def seed(directory):
    """Archive a real solved 48-hour example without connecting providers or physical equipment.

    A 4-kW midday solar curve, morning/evening household peaks and higher evening
    rates create visible import, export, charge and discharge. The 10-kWh battery
    starts at 4 kWh with a 1-kWh reserve. Validation uses the normal independent
    evaluator before any plan is written. These are illustrative inputs, not
    independently measured generation or a forecast-accuracy claim.
    """
    schedule = ContractSchedule(
        "documentation-example",
        "NL",
        "EUR",
        START - timedelta(days=1),
        (
            ContractPeriod(
                START - timedelta(days=1), None, Formula("market + 0.10"), Formula("market")
            ),
        ),
    )
    intervals, tariffs = [], []
    for start, hours in planning_grid(START, align=False):
        hour = start.hour + start.minute / 60
        solar_kw = 4 * max(0, sin(pi * (hour - 7) / 10)) if 7 < hour < 17 else 0
        load_kw = 2.2 if 7 <= hour < 9 else 2.7 if 17 <= hour < 21 else 0.45
        market = 0.32 if 17 <= hour < 21 else 0.02 if hour < 6 else 0.08
        intervals.append(
            Interval(
                start,
                hours,
                market + 0.10,
                market,
                load_kw * hours,
                solar_kw * hours,
                pv_sources=(("roof", solar_kw * hours),),
            )
        )
        tariffs.append(
            TariffEvidence(
                start,
                start + timedelta(hours=hours),
                market,
                market + 0.10,
                market,
                "published" if start < START + timedelta(hours=24) else "forecast",
                15 if hours == 0.25 else 60,
                "synthetic-teaching-prices",
                "documentation-prices",
                schedule.version,
                0,
            )
        )
    problem = InstallationProblem(
        tuple(intervals),
        (SolarInverter("hybrid", 5000, 0.96, True),),
        (BatteryState("battery", "hybrid", Battery(10, 4, 1, 3000, 3000, 0.95, 0.95)),),
        (PVUnit("roof", "hybrid", False),),
        6000,
        6000,
        terminal_valuations=(
            TerminalValuation(
                "battery",
                1,
                9,
                0.20,
                START + timedelta(hours=48),
                START + timedelta(hours=72),
                "post_horizon_prices",
            ),
        ),
    )
    snapshot = InputSnapshot(
        "documentation-example",
        "documentation-installation",
        START,
        "documentation-measurements",
        "documentation-forecasts",
        "documentation-prices",
        problem,
        contract_version=schedule.version,
        tariff_evidence=tuple(tariffs),
    )
    result = HighsSolver().solve(problem, SolveOptions(30))
    assert result.plan is not None, result.status
    costs = validate(problem, result.plan)
    document = plan_document(snapshot, result, costs, START)
    document.update(
        currency="EUR",
        market_area="NL",
        comparison_names={"battery": "Example home battery", "roof": "Example south roof"},
    )
    repository = SqlPlanRepository(directory)
    try:
        repository.state["plan"] = document
        repository.save()
        ContractStore(SqlDocuments(repository.engine, "contracts")).save(schedule)
    finally:
        repository.close()


def quality_document():
    """Describe a fixed illustrative day with a shifted evening peak and complete coverage."""
    beginning = START - timedelta(hours=1)
    predicted = [0.3] * 6 + [0.8] * 3 + [0.4] * 8 + [1.4] * 4 + [0.5] * 3
    measured = [0.3] * 6 + [0.6, 0.9, 0.8] + [0.4] * 8 + [0.8, 1.7, 1.6, 1.2] + [0.5] * 3
    rows = [
        {
            "start": (beginning + timedelta(hours=hour)).isoformat(),
            "end": (beginning + timedelta(hours=hour + 1)).isoformat(),
            "predicted_kwh": prediction,
            "measured_kwh": observation,
            "error_kwh": prediction - observation,
            "evaluation_issue": None,
        }
        for hour, (prediction, observation) in enumerate(zip(predicted, measured, strict=True))
    ]
    errors = [
        prediction - observation
        for prediction, observation in zip(predicted, measured, strict=True)
    ]
    return {
        "day": "2026-11-04",
        "start": beginning.isoformat(),
        "end": (beginning + timedelta(days=1)).isoformat(),
        "captured_at": beginning.isoformat(),
        "as_of": (START + timedelta(days=2)).isoformat(),
        "timezone": "Europe/Amsterdam",
        "issues": [],
        "current_models": {},
        "series": [
            {
                "id": "household",
                "title": "Household consumption",
                "model_label": "Example weekday model",
                "forecast_version": "synthetic-midnight-example",
                "issued_at": beginning.isoformat(),
                "last_received_at": (START + timedelta(days=1)).isoformat(),
                "rows": rows,
                "metrics": {
                    "mae_kwh": sum(abs(error) for error in errors) / 24,
                    "bias_kwh": sum(errors) / 24,
                    "rmse_kwh": (sum(error * error for error in errors) / 24) ** 0.5,
                    "wape_percent": 100 * sum(abs(error) for error in errors) / sum(measured),
                },
                "daily": {
                    "predicted_kwh": sum(predicted),
                    "measured_kwh": sum(measured),
                    "error_kwh": sum(errors),
                    "relative_error_percent": 100 * abs(sum(errors)) / sum(measured),
                    "evaluation_issue": None,
                },
                "coverage": {"hours": 24, "measured_hours": 24, "paired_hours": 24},
            }
        ],
    }
