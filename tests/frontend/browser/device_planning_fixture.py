"""Build a real solved two-day plan with individual expected and acknowledged appliance demand."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from open_energy_planner.appliances.cycles.service import ApplianceService
from open_energy_planner.appliances.forecasting import ConsumerForecasts
from open_energy_planner.installation.commands import plan_document
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import SolveOptions
from open_energy_planner.planning import InputSnapshot
from open_energy_planner.savings.context import physical_context
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.documentation_fixture import seed as seed_household

DEVICE_NAMES = {
    "sensor.washer_energy": "Washing machine",
    "sensor.dryer_energy": "Dryer",
    "sensor.dishwasher_energy": "Dishwasher",
    "sensor.charger_energy": "Charge point",
}


def seed(directory, *, fractional_boundaries=False, legend_devices=False):
    """Keep full-day solar and prices, and solve explicit washer/dryer tasks with remaining expectations.

    Args:
        directory: Empty app data directory receiving the validated archived plan.
        fractional_boundaries: Shift ready times forward and deadlines backward
            by 123456 microseconds, and fix acknowledged starts at ready_at.
            This isolates boundary precision from free-scheduling search. The
            default retains ordinary free scheduling over the full 48 hours.
        legend_devices: Include an unused meter and an expected-only device
            whose consumption starts exactly at the second day's boundary.
    """
    seed_household(directory)
    repository = SqlPlanRepository(directory)
    try:
        original = repository.state["plan"]
        problem = physical_context(original)
        consumers = ConsumerForecasts(SqlDocuments(repository.engine, "demand"))
        names = dict(DEVICE_NAMES)
        if legend_devices:
            names.update(
                {"sensor.idle_energy": "Unused appliance", "sensor.later_energy": "Later appliance"}
            )
        consumers.sync(
            {
                "entities": [{"entity_id": entity, "name": name} for entity, name in names.items()],
                "suggestions": {
                    "consumers": [{"entity_id": entity, "upstream": None} for entity in names]
                },
            }
        )
        consumers.save(
            [{"entity_id": entity, "weather_sensitive": False} for entity in names],
            consumers.version,
        )

        def forecast(rates=None):
            """Freeze independently declared mean AC rates without replacing unknown hours with zeros."""
            return {
                "issued_at": START.isoformat(),
                "steps": [
                    {
                        "start": step.start.isoformat(),
                        "duration_h": step.duration_h,
                        "kwh": (rates(step) if rates else step.load_kwh / step.duration_h - 0.24)
                        * step.duration_h,
                    }
                    for step in problem.intervals
                ],
            }

        def later_rate(step):
            """Shift 30 W from household base to a named device only on the second day."""
            return 0.03 if legend_devices and step.start >= START + timedelta(hours=24) else 0

        residual = forecast(lambda step: step.load_kwh / step.duration_h - 0.24 - later_rate(step))
        consumers.result = {"devices": [], "residual": residual, "residual_baseline": residual}
        for entity, power_kw in zip(DEVICE_NAMES, (0.04, 0.06, 0.04, 0.10), strict=True):
            expected = forecast(lambda _step, rate=power_kw: rate)
            consumers.result["devices"].append(
                {"entity_id": entity, "forecast": expected, "baseline_forecast": expected}
            )
        if legend_devices:
            for entity, rates in (
                ("sensor.idle_energy", lambda _step: 0),
                ("sensor.later_energy", later_rate),
            ):
                expected = forecast(rates)
                consumers.result["devices"].append(
                    {"entity_id": entity, "forecast": expected, "baseline_forecast": expected}
                )
        appliances = ApplianceService(SqlDocuments(repository.engine, "planning"), consumers)
        for entity, duration, energy in (
            ("sensor.washer_energy", 90, 0.9),
            ("sensor.dryer_energy", 120, 1.8),
        ):
            appliances.configure(entity, True)
            appliances.set_manual_profile(entity, duration, energy, appliances.revision)
            appliances.submit(
                entity,
                "synthetic-ready-" + entity,
                START + timedelta(hours=8, microseconds=123456 if fractional_boundaries else 0),
                START + timedelta(hours=17, microseconds=-123456 if fractional_boundaries else 0),
                START,
            )
        snapshot = InputSnapshot(
            "device-example",
            "example-installation",
            START,
            "example-meters",
            "example-forecasts",
            "example-prices",
            problem,
            contract_version=original["input_snapshot"]["contract_version"],
            tariff_evidence=tuple(original["input_snapshot"]["tariff_evidence"]),
        )
        snapshot = appliances.prepare(
            snapshot,
            SimpleNamespace(forecast=SimpleNamespace(model="consumer-composite-v1")),
            START,
        )
        assert len(snapshot.problem.appliances) == 2
        assert len(snapshot.load_components) == (7 if legend_devices else 5)
        if fractional_boundaries:
            # Freeze acknowledged starts to test boundary precision independently of timing search.
            snapshot = replace(
                snapshot,
                problem=replace(
                    snapshot.problem,
                    appliances=tuple(
                        replace(work, fixed_start=work.ready_at)
                        for work in snapshot.problem.appliances
                    ),
                ),
            )
        solution = HighsSolver().solve(snapshot.problem, SolveOptions(30))
        assert solution.plan is not None, (solution.status, solution.stop_reason)
        costs = validate(snapshot.problem, solution.plan)
        document = plan_document(snapshot, solution, costs, START + timedelta(seconds=1))
        document.update(
            currency="EUR",
            market_area="NL",
            comparison_names={**original["comparison_names"], **names},
        )
        repository.state["plan"] = document
        repository.save()
    finally:
        repository.close()
