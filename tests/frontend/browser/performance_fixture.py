"""Persist explicitly synthetic calculation evidence for real responsive API/browser acceptance."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from open_energy_planner.optimization.domain import SolveResult, SolveSample, Status
from open_energy_planner.optimization.performance import CalculationHistory
from open_energy_planner.planning import PlanningOutcome
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from open_energy_planner.synthetic import capture


def seed(directory):
    """Create three comparable-looking runs with distinct measured timing and proof outcomes.

    These are independent UI inputs, not solver performance claims. The final run
    lacks a first-candidate observation. Yesterday has a single zero-cost result.
    """
    repository = SqlPlanRepository(directory)
    history = CalculationHistory(SqlDocuments(repository.engine, "planning"))
    today = (
        datetime.now(ZoneInfo("Europe/Amsterdam"))
        .replace(hour=8, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )
    for index, (duration, first, reason) in enumerate(
        ((22, 4, "optimal"), (120.2, 12, "time_limit"), (8, None, "optimal"))
    ):
        started = today + timedelta(minutes=15 * index)
        result = SolveResult(
            Status.FEASIBLE if reason == "time_limit" else Status.OPTIMAL,
            None,
            reason,
            0.2,
            duration - 0.2,
            -0.51,
            progress=(
                SolveSample(first or duration, -0.44, -0.6),
                SolveSample(duration, -0.5, -0.51),
            ),
            financial_started_seconds=1,
            first_candidate_seconds=first,
            time_limit_seconds=120,
        )
        history.record(
            capture(started),
            PlanningOutcome(
                {"plan_id": f"fixture-plan-{index}", "currency": "EUR"}, "validated", result
            ),
            started,
            started + timedelta(seconds=duration),
            "published",
        )
    started = today - timedelta(days=1)
    history.record(
        capture(started),
        PlanningOutcome(
            None,
            "candidate_rejected",
            SolveResult(Status.ERROR, None, "native_run_error", 0, 0, None),
        ),
        started,
        started + timedelta(seconds=2),
        "candidate_rejected",
    )
    repository.close()
