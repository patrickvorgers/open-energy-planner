"""One native solver lifecycle. No device physics, IO or publication policy."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from time import perf_counter

import highspy

from open_energy_planner.installation.model import InstallationProblem
from open_energy_planner.optimization.adapters.highs.consumption import prioritize_consumption
from open_energy_planner.optimization.adapters.highs.installation import InstallationModelFactory
from open_energy_planner.optimization.adapters.highs.progress import FinancialProgress
from open_energy_planner.optimization.adapters.highs.recovery import (
    prioritize_reserve,
    set_time_budget,
)
from open_energy_planner.optimization.domain import (
    SolveOptions,
    SolveResult,
    Status,
    Strategy,
)

NATIVE_OPTIONS = {
    "threads": 1,
    "random_seed": 0,
    "mip_rel_gap": 0,
    "mip_abs_gap": 0,
    "primal_feasibility_tolerance": 1e-8,
    "mip_feasibility_tolerance": 1e-8,
}
FINANCIAL_MIP_RELATIVE_GAP = 0.005


@dataclass(frozen=True)
class SolveDisposition:
    """Map native termination and candidate availability to explicit domain result semantics."""

    status: Status
    reason: str
    use_candidate: bool

    @classmethod
    def from_native(cls, model_status, candidate: bool):
        """Distinguish optimal, interrupted-feasible and no-candidate outcomes without inventing optimality."""
        ms = highspy.HighsModelStatus
        terminal = {
            ms.kOptimal: (Status.OPTIMAL, "optimal"),
            ms.kInfeasible: (Status.INFEASIBLE, "infeasible"),
            ms.kUnbounded: (Status.UNBOUNDED, "unbounded"),
        }
        if model_status in terminal:
            status, reason = terminal[model_status]
            if status == Status.OPTIMAL and not candidate:
                return cls(Status.ERROR, "optimal_without_candidate", False)
            return cls(status, reason, status == Status.OPTIMAL)
        stopped = {
            ms.kTimeLimit: "time_limit",
            ms.kIterationLimit: "iteration_limit",
            ms.kSolutionLimit: "solution_limit",
            ms.kInterrupt: "interrupted",
            ms.kObjectiveBound: "objective_bound",
            ms.kObjectiveTarget: "objective_target",
            ms.kMemoryLimit: "memory_limit",
            ms.kHighsInterrupt: "interrupted",
        }
        if model_status in stopped:
            return cls(
                Status.FEASIBLE if candidate else Status.NO_CANDIDATE,
                stopped[model_status],
                candidate,
            )
        return cls(Status.ERROR, "ambiguous_or_native_error", False)


class HighsSolver:
    """Build and solve an isolated HiGHS model behind the domain solver port."""

    def __init__(self, factory=None):
        """Build current installation models, allowing explicit construction fault injection in tests."""
        self.factory = factory if factory is not None else InstallationModelFactory()

    def solve(
        self,
        problem: InstallationProblem,
        options: SolveOptions | None = None,
    ) -> SolveResult:
        """Return a candidate and native evidence without publishing device commands.

        Extracted candidates require independent physical and financial validation.
        Known build, native and extraction failures return an error without a
        partial plan. Unproved service priorities also return no candidate.
        Timings and the retained shared preparation/service/search budget are
        seconds. The objective and bound use the problem's contract currency.
        """
        options = options or SolveOptions()
        started_at = datetime.now(UTC)
        return replace(
            self._solve(problem, options),
            time_limit_seconds=options.time_limit_s,
            started_at=started_at,
        )

    def _solve(
        self,
        problem: InstallationProblem,
        options: SolveOptions | None = None,
    ) -> SolveResult:
        """Return a candidate, termination reason, timings and bound without publishing commands.

        Known build/native/extraction failures return an error without a partial plan.
        Extracted candidates still require independent physical and financial validation.
        The deadline starts on entry and includes model construction. Reserve
        recovery precedes thermal comfort and ending-stock service, appliance
        service, then EV service by earliest departure.
        An unproved service stage returns NO_CANDIDATE with its stop reason,
        including ev_service_time_limit, ev_service_error or ev_service_unproven.
        Consumption first proves corrected minimum grid-side kWh after reserve
        recovery, then fixes it for the financial solve. Every stage shares the
        deadline. An unproved primary stage returns no candidate and an explicit
        consumption_* stop reason. consumption_optimum_kwh is present only with
        proved primary evidence. FEASIBLE means the financial tie-breaker stopped
        with a candidate, not that either priority was silently relaxed.
        Only the final financial stage allows a 0.5 percent relative MIP gap.
        Retain the native dual bound even when that tolerance reports optimal.
        """
        options = options or SolveOptions()
        started, build_seconds, solve_seconds = perf_counter(), 0.0, 0.0
        try:
            model = self.factory.build(problem)
            native = model.context.native
            for key, value in dict(NATIVE_OPTIONS, time_limit=options.time_limit_s).items():
                if native.setOptionValue(key, value) != highspy.HighsStatus.kOk:
                    raise ValueError(f"Rejected native option: {key}")
            build_seconds = perf_counter() - started
            solving = perf_counter()
            deadline = started + options.time_limit_s
            recovery_issue = prioritize_reserve(model, problem, deadline)
            if recovery_issue:
                return SolveResult(
                    {
                        "reserve_recovery_infeasible": Status.INFEASIBLE,
                        "reserve_recovery_native_error": Status.ERROR,
                        "reserve_recovery_invalid_objective": Status.ERROR,
                    }.get(recovery_issue, Status.NO_CANDIDATE),
                    None,
                    recovery_issue,
                    build_seconds,
                    perf_counter() - solving,
                    None,
                )
            consumption_optimum = None
            from .thermal import prioritize_service as prioritize_thermal

            thermal_issue = prioritize_thermal(model, problem, deadline)
            if thermal_issue:
                return SolveResult(
                    Status.NO_CANDIDATE,
                    None,
                    thermal_issue,
                    build_seconds,
                    perf_counter() - solving,
                    None,
                )
            from .appliances import prioritize_tasks

            appliance_issue = prioritize_tasks(model, problem, deadline)
            if appliance_issue:
                return SolveResult(
                    Status.NO_CANDIDATE,
                    None,
                    appliance_issue,
                    build_seconds,
                    perf_counter() - solving,
                    None,
                )
            from .ev import prioritize_service

            ev_issue = prioritize_service(model, problem, deadline)
            if ev_issue:
                return SolveResult(
                    Status.NO_CANDIDATE,
                    None,
                    ev_issue,
                    build_seconds,
                    perf_counter() - solving,
                    None,
                )
            if options.strategy == Strategy.CONSUMPTION:
                consumption_optimum, issue = prioritize_consumption(model, problem, deadline)
                if issue:
                    return SolveResult(
                        Status.INFEASIBLE
                        if issue == "consumption_infeasible"
                        else Status.ERROR
                        if issue in ("consumption_native_error", "consumption_invalid_objective")
                        else Status.NO_CANDIDATE,
                        None,
                        issue,
                        build_seconds,
                        perf_counter() - solving,
                        None,
                    )
            remaining = deadline - perf_counter()
            if remaining <= 0:
                return SolveResult(
                    Status.NO_CANDIDATE,
                    None,
                    "time_limit",
                    build_seconds,
                    perf_counter() - solving,
                    None,
                )
            if (
                native.setOptionValue("mip_rel_gap", FINANCIAL_MIP_RELATIVE_GAP)
                != highspy.HighsStatus.kOk
            ):
                raise ValueError("Rejected financial optimality tolerance")
            set_time_budget(native, remaining)
            financial_started = perf_counter() - started
            progress = FinancialProgress(started)
            progress.subscribe(native)
            run_status = native.run()
            solve_seconds = perf_counter() - solving
            if run_status == highspy.HighsStatus.kError:
                return SolveResult(
                    Status.ERROR, None, "native_run_error", build_seconds, solve_seconds, None
                )
            info, solution = native.getInfo(), native.getSolution()
            candidate = bool(
                solution.value_valid
                and info.primal_solution_status == highspy.SolutionStatus.kSolutionStatusFeasible
            )
            disposition = SolveDisposition.from_native(native.getModelStatus(), candidate)
            bound = info.mip_dual_bound if info.mip_node_count >= 0 else None
            bound = float(bound) if bound is not None and isfinite(bound) else None
            plan = model.extract() if disposition.use_candidate else None
            if disposition.status == Status.OPTIMAL and bound is None:
                bound = float(native.getObjectiveValue())
            if candidate:
                progress.finish(
                    float(native.getObjectiveValue()), bound if bound is not None else float("nan")
                )
            return SolveResult(
                disposition.status,
                plan,
                disposition.reason,
                build_seconds,
                solve_seconds,
                bound,
                consumption_optimum,
                tuple(progress.samples),
                financial_started,
                progress.first_candidate_seconds,
            )
        except (RuntimeError, ValueError, TypeError, IndexError, KeyError) as exc:
            # No partial candidate escapes build, native or extraction failures.
            elapsed = perf_counter() - started
            return SolveResult(
                Status.ERROR,
                None,
                f"adapter_error:{type(exc).__name__}",
                build_seconds or elapsed,
                solve_seconds,
                None,
            )
