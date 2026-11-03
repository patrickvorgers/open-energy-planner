"""Publication acceptance tests with independent economics and temporal fault injection."""

import asyncio
import logging
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.open_energy_planner.protocol import Plan
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.adapters.process.worker import ProcessSolver
from open_energy_planner.optimization.domain import Costs, SolveResult, Status
from open_energy_planner.planning import PlanningCoordinator, PlanningService, PublicationSuperseded
from open_energy_planner.synthetic import capture

NOW = datetime(2026, 10, 19, tzinfo=UTC)


class ImmediateSolver:
    """The real selected solver behind the asynchronous use-case port."""

    async def solve(self, problem):
        """Expose real HiGHS arithmetic through the asynchronous port for deterministic use-case tests."""
        return HighsSolver().solve(problem)


def service(solver=None, clock=None):
    """Build a planning use case with the explicit solver and deterministic clock fixtures."""
    return PlanningService(solver or ImmediateSolver(), clock=clock or (lambda: NOW))


def test_frozen_snapshot_has_stable_hash_and_no_mutable_collections():
    """Attempt input and exported-record mutation. Require frozen originals, stable hashes and the fixed 120-step horizon."""
    snapshot = capture(NOW)
    with pytest.raises(FrozenInstanceError):
        snapshot.problem.batteries[0].battery.initial_kwh = 0.5
    before = snapshot.digest
    record = snapshot.record()
    record["problem"]["batteries"][0]["battery"]["initial_kwh"] = 0.5
    assert snapshot.digest == before
    assert len(snapshot.problem.intervals) == 120
    assert sum(interval.duration_h for interval in snapshot.problem.intervals) == 48


def test_real_worker_validates_48_hour_plan_and_preserves_input_and_costs():
    """Run the real worker. Require -0.28 cost because buying 1 kWh at 0.12 then selling at 0.40 earns that spread."""

    async def scenario():
        snapshot = capture(NOW)
        outcome = await service(ProcessSolver()).calculate(snapshot)
        assert outcome.reason == "validated"
        document = outcome.document
        assert document["independent_costs"]["objective"] == pytest.approx(-0.28)
        assert document["steps"][0]["devices"]["battery"]["power_w"] == -4000
        assert document["steps"][1]["devices"]["battery"]["power_w"] == 4000
        assert document["input_snapshot_id"] == snapshot.snapshot_id
        assert document["input_sha256"] == snapshot.digest
        assert document["input_snapshot"] == snapshot.record()
        assert Plan.parse(document, "demo-1").strategy == "minimize cost"

    asyncio.run(scenario())


def test_solver_generation_after_first_interval_start_is_a_valid_document():
    """Finish solving after the first step starts. Accept the document while current coverage and freshness still hold."""

    async def scenario():
        outcome = await service(clock=lambda: NOW + timedelta(seconds=2)).calculate(capture(NOW))
        assert outcome.reason == "validated"
        assert Plan.parse(outcome.document, "demo-1").generated_at > NOW

    asyncio.run(scenario())


def test_terminal_side_power_applies_converter_and_cell_losses_once():
    """Apply converter/cell losses. Require -3600/2592 terminal watts and -0.11328 cost from 0.5832 AC kWh sold at 0.40."""

    async def scenario():
        snapshot = capture(NOW)
        problem = replace(
            snapshot.problem,
            batteries=(
                replace(
                    snapshot.problem.batteries[0],
                    battery=replace(
                        snapshot.problem.batteries[0].battery,
                        charge_efficiency=0.8,
                        discharge_efficiency=0.9,
                    ),
                ),
            ),
            inverters=(replace(snapshot.problem.inverters[0], efficiency=0.9),),
        )
        outcome = await service(ProcessSolver()).calculate(replace(snapshot, problem=problem))
        assert outcome.reason == "validated"
        assert outcome.document["steps"][0]["devices"]["battery"]["power_w"] == -3600
        assert outcome.document["steps"][1]["devices"]["battery"]["power_w"] == 2592
        assert outcome.document["independent_costs"]["objective"] == pytest.approx(-0.11328)

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["cost", "state", "no_candidate", "infeasible"])
def test_invalid_or_missing_candidates_never_reach_publication(mutation):
    """Forge costs/state or return no feasible candidate. Require independent rejection and zero publication calls."""

    async def scenario():
        class FaultySolver:
            async def solve(self, problem):
                result = HighsSolver().solve(problem)
                if mutation == "cost":
                    return replace(
                        result,
                        plan=replace(result.plan, costs=replace(result.plan.costs, objective=-100)),
                    )
                if mutation == "state":
                    return replace(
                        result,
                        plan=replace(
                            result.plan,
                            flows=(
                                replace(
                                    result.plan.flows[0],
                                    batteries=(
                                        replace(result.plan.flows[0].batteries[0], end_kwh=0.1),
                                    ),
                                ),
                                *result.plan.flows[1:],
                            ),
                        ),
                    )
                return SolveResult(
                    Status.INFEASIBLE if mutation == "infeasible" else Status.NO_CANDIDATE,
                    None,
                    mutation,
                    0,
                    0,
                    None,
                )

        published = []

        async def publish(document):
            published.append(document)

        coordinator = PlanningCoordinator(service(FaultySolver()), publish)
        coordinator.submit(capture(NOW))
        await coordinator.wait()
        assert coordinator.status == "failed"
        assert published == []

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["old", "future", "strategy"])
def test_stale_future_or_unsupported_input_is_not_solved(kind):
    """Supply stale/future measurements or unsupported strategy. Reject before invoking the solver."""

    async def scenario():
        class NeverSolver:
            async def solve(self, problem):
                raise AssertionError("Bad input reached the solver")

        snapshot = capture(NOW)
        if kind == "old":
            snapshot = replace(snapshot, measured_at=NOW - timedelta(seconds=61))
        elif kind == "future":
            snapshot = replace(snapshot, measured_at=NOW + timedelta(seconds=1))
        else:
            snapshot = replace(snapshot, strategy="unsupported strategy")
        outcome = await service(NeverSolver()).calculate(snapshot)
        assert outcome.document is None
        assert outcome.reason in ("stale_input", "unsupported_strategy")

    asyncio.run(scenario())


def test_input_expiring_during_solve_is_rejected_after_validation():
    """Advance the clock past freshness during calculation. Reject the otherwise feasible result before publication."""

    async def scenario():
        times = iter([NOW, NOW + timedelta(seconds=61)])
        outcome = await service(clock=lambda: next(times)).calculate(capture(NOW))
        assert outcome.document is None
        assert outcome.reason == "input_expired_during_solve"

    asyncio.run(scenario())


def test_fractional_command_is_rejected_instead_of_silently_rounded():
    """Use capacity requiring fractional watts. Reject projection because rounding would change the validated physical plan."""

    async def scenario():
        snapshot = capture(NOW)
        snapshot = replace(
            snapshot,
            problem=replace(
                snapshot.problem,
                batteries=(
                    replace(
                        snapshot.problem.batteries[0],
                        battery=replace(
                            snapshot.problem.batteries[0].battery, capacity_kwh=0.123456
                        ),
                    ),
                ),
            ),
        )

        class FractionalSolver:
            """Inject a fractional terminal quantity that no current command may publish."""

            async def solve(self, problem):
                """Keep the independently specified fractional candidate distinct from solver quantization."""
                result = HighsSolver().solve(problem)
                candidate_flows = []
                for interval_index, interval_flow in enumerate(result.plan.flows):
                    charge_kwh = 0.123456 if interval_index == 0 else 0
                    discharge_kwh = 0.123456 if interval_index == 1 else 0
                    end_kwh = 0.123456 if interval_index == 0 else 0
                    candidate_flows.append(
                        replace(
                            interval_flow,
                            grid_import=charge_kwh,
                            grid_export=discharge_kwh,
                            batteries=(
                                replace(
                                    interval_flow.batteries[0],
                                    charge_kwh=charge_kwh,
                                    discharge_kwh=discharge_kwh,
                                    end_kwh=end_kwh,
                                ),
                            ),
                            inverters=(
                                replace(
                                    interval_flow.inverters[0],
                                    import_kwh=charge_kwh,
                                    export_kwh=discharge_kwh,
                                ),
                            ),
                        )
                    )
                # .123456 kWh bought at .12 and sold at .40 has -.03456768 cash cost.
                costs = Costs(0.01481472, 0.0493824, 0, 0, -0.03456768, 0, -0.03456768)
                candidate = replace(result.plan, flows=tuple(candidate_flows), costs=costs)
                with pytest.raises(ValueError, match="Whole-watt command"):
                    validate(problem, candidate)
                return replace(result, plan=candidate)

        outcome = await service(FractionalSolver()).calculate(snapshot)
        assert outcome.document is None
        assert outcome.reason == "candidate_rejected"

    asyncio.run(scenario())


def test_new_inputs_are_coalesced_and_infeasible_old_dispatch_cannot_be_published():
    """New SoC makes the original full-charge plan exceed capacity. Publish only the latest independently validated result."""

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls, published = [], []

        class DelayedSolver:
            async def solve(self, problem):
                calls.append(problem.batteries[0].battery.initial_kwh)
                started.set()
                await release.wait()
                return HighsSolver().solve(problem)

        async def publish(document):
            published.append(document)

        coordinator = PlanningCoordinator(service(DelayedSolver()), publish)
        first = capture(NOW, 0)
        coordinator.submit(first)
        await started.wait()
        coordinator.submit(capture(NOW, 0.25))
        latest = capture(NOW, 0.5)
        coordinator.submit(latest)
        release.set()
        await coordinator.wait()
        assert calls == [0, 0.5]
        assert len(published) == 1
        assert published[0]["input_snapshot_id"] == latest.snapshot_id
        assert first.problem.batteries[0].battery.initial_kwh == 0
        assert published[0]["steps"][0]["devices"]["battery"]["power_w"] == -2000

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["calculate", "publish"])
def test_invalidated_input_reason_survives_a_failing_calculation_or_publication(stage):
    """Invalidate input before an injected failure completes. Retain the actionable not-ready reason rather than generic failure."""

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def fail_after_invalidation():
            started.set()
            await release.wait()
            raise OSError("Interrupted stale planning round")

        class FailingService:
            async def calculate(self, snapshot):
                await fail_after_invalidation()

        async def publish(document):
            await fail_after_invalidation()

        coordinator = PlanningCoordinator(
            FailingService() if stage == "calculate" else service(), publish
        )
        coordinator.submit(capture(NOW))
        await started.wait()
        coordinator.invalidate("required_measurement_unavailable")
        release.set()
        await coordinator.wait()
        assert coordinator.latest is None
        assert (coordinator.status, coordinator.reason) == (
            "not_ready",
            "required_measurement_unavailable",
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", ["new_snapshot", "invalidated", "expired"])
def test_publication_supersession_preserves_latest_inputs_and_readiness(replacement, caplog):
    """Reject a stale publication normally. Process newer input or preserve an explicit not-ready reason."""

    async def scenario():
        """Inject supersession inside publication, after the coordinator's generation check."""
        published, attempted = [], []
        latest = capture(NOW, 0.5)

        async def publish(document):
            """Supersede the first candidate and accept only a subsequently calculated generation."""
            attempted.append(document)
            if len(attempted) == 1:
                if replacement == "new_snapshot":
                    coordinator.submit(latest)
                elif replacement == "invalidated":
                    coordinator.invalidate("required_measurement_unavailable")
                raise PublicationSuperseded
            published.append(document)

        coordinator = PlanningCoordinator(service(), publish)
        coordinator.submit(capture(NOW))
        await coordinator.wait()
        if replacement == "new_snapshot":
            assert len(attempted) == 2
            assert [item["input_snapshot_id"] for item in published] == [latest.snapshot_id]
            assert (coordinator.status, coordinator.reason) == ("ready", "validated")
        else:
            assert len(attempted) == 1 and not published
            assert coordinator.latest is None
            assert (coordinator.status, coordinator.reason) == (
                "not_ready",
                "required_measurement_unavailable"
                if replacement == "invalidated"
                else "publication_inputs_expired",
            )

    with caplog.at_level(logging.INFO, logger="open_energy_planner.planning"):
        asyncio.run(scenario())
    discards = [
        record for record in caplog.records if "Plan discarded before publication" in record.message
    ]
    assert len(discards) == 1
    assert all(
        record.levelno == logging.INFO and record.exc_info is None for record in caplog.records
    )


def test_current_publication_failure_remains_a_planning_error(caplog):
    """Fail persistence for the current valid generation. Report planning_error because no newer invalidation supersedes it."""

    async def scenario():
        async def publish(document):
            raise OSError("Persistence unavailable")

        coordinator = PlanningCoordinator(service(), publish)
        coordinator.submit(capture(NOW))
        await coordinator.wait()
        assert (coordinator.status, coordinator.reason) == ("failed", "planning_error")

    asyncio.run(scenario())
    failures = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(failures) == 1
    assert failures[0].exc_info[0] is OSError


def test_native_worker_deadline_does_not_prevent_a_later_successful_worker():
    """Time out one child process. Require a later independent worker to solve successfully."""

    async def scenario():
        snapshot = capture(NOW)
        stopped = await ProcessSolver(timeout_s=0.001).solve(snapshot.problem)
        assert stopped.plan is None and stopped.stop_reason == "worker_timeout"
        result = await ProcessSolver().solve(snapshot.problem)
        assert result.status == Status.OPTIMAL

    asyncio.run(scenario())


def test_default_worker_passes_two_minute_budget_with_process_overhead(monkeypatch):
    """Forward 120 solver seconds under a 125-second process bound without waiting for either limit."""
    from open_energy_planner.optimization.adapters.process import worker

    expected_result = SolveResult(Status.NO_CANDIDATE, None, "time_limit", 0, 120, None)

    def bounded_child(target, problem, timeout_s, solver_time_limit_s, strategy):
        """Inspect the actual child invocation at the production process boundary."""
        assert target is worker.solve_in_child
        assert timeout_s == 125
        assert solver_time_limit_s == 120
        return expected_result

    monkeypatch.setattr(worker, "run_child", bounded_child)
    assert asyncio.run(ProcessSolver().solve(capture(NOW).problem)) is expected_result


def test_worker_models_whole_watts_without_exceeding_fractional_capacity():
    """Use fractional capacity. Require integer dispatch of 493 W and -0.03451 cost because 0.12325 kWh captures a 0.28 spread."""

    async def scenario():
        snapshot = capture(NOW)
        snapshot = replace(
            snapshot,
            problem=replace(
                snapshot.problem,
                batteries=(
                    replace(
                        snapshot.problem.batteries[0],
                        battery=replace(
                            snapshot.problem.batteries[0].battery, capacity_kwh=0.123456
                        ),
                    ),
                ),
            ),
        )
        outcome = await service(ProcessSolver()).calculate(snapshot)
        assert outcome.reason == "validated"
        assert outcome.document["steps"][0]["devices"]["battery"]["power_w"] == -493
        assert outcome.document["steps"][1]["devices"]["battery"]["power_w"] == 493
        assert outcome.document["independent_costs"]["objective"] == pytest.approx(-0.03451)

    asyncio.run(scenario())


@pytest.mark.parametrize("index", [0, 1, 2])
def test_production_worker_retains_known_cost_negative_price_and_terminal_cases(index):
    """Run independent core cases through the spawned worker. Preserve spread, paid-charge and final-stock economics."""
    from open_energy_planner.installation.validation import validate
    from tests.app.solver_regression.cases import CORE

    async def scenario():
        case = CORE[index]
        result = await ProcessSolver().solve(case.problem)
        assert result.status == Status.OPTIMAL
        costs = validate(case.problem, result.plan)
        assert costs.cash_cost == pytest.approx(float(case.cash))
        assert costs.terminal_value == pytest.approx(float(case.terminal))
        assert costs.objective == pytest.approx(float(case.objective))

    asyncio.run(scenario())
