"""Native stop quality, candidate gating and no partial results on failures."""

from types import SimpleNamespace

import highspy
import pytest

from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.adapters.highs.installation import InstallationModelFactory
from open_energy_planner.optimization.adapters.highs.solver import SolveDisposition
from open_energy_planner.optimization.domain import SolveOptions, Status
from tests.app.solver_regression.cases import CORE

MS = highspy.HighsModelStatus


@pytest.mark.parametrize(
    ("native_status", "candidate", "status", "reason"),
    [
        (MS.kOptimal, True, Status.OPTIMAL, "optimal"),
        (MS.kOptimal, False, Status.ERROR, "optimal_without_candidate"),
        (MS.kInfeasible, True, Status.INFEASIBLE, "infeasible"),
        (MS.kUnbounded, True, Status.UNBOUNDED, "unbounded"),
        (MS.kUnboundedOrInfeasible, True, Status.ERROR, "ambiguous_or_native_error"),
        (MS.kTimeLimit, True, Status.FEASIBLE, "time_limit"),
        (MS.kTimeLimit, False, Status.NO_CANDIDATE, "time_limit"),
        (MS.kIterationLimit, True, Status.FEASIBLE, "iteration_limit"),
        (MS.kSolutionLimit, False, Status.NO_CANDIDATE, "solution_limit"),
        (MS.kInterrupt, True, Status.FEASIBLE, "interrupted"),
        (MS.kSolveError, True, Status.ERROR, "ambiguous_or_native_error"),
    ],
)
def test_stop_status_does_not_claim_optimality_or_use_stale_candidate(
    native_status, candidate, status, reason
):
    """Map controlled native stops. Return a candidate only for optimal/feasible outcomes with explicit status."""
    result = SolveDisposition.from_native(native_status, candidate)
    assert (result.status, result.reason) == (status, reason)
    assert result.use_candidate == (status in (Status.OPTIMAL, Status.FEASIBLE))


def test_real_immediate_deadline_never_returns_an_unvalidated_partial_plan():
    """Set a near-zero native deadline. Require time-limit/no-candidate rather than partial plan publication."""
    result = HighsSolver().solve(CORE[0].problem, SolveOptions(time_limit_s=1e-9))
    assert result.status == Status.NO_CANDIDATE
    assert result.stop_reason == "time_limit"
    assert result.plan is None


class NativeView:
    """Controlled stop/error reporting over real native model/candidate arithmetic."""

    def __init__(self, native, *, status=MS.kTimeLimit, fail=False):
        """Wrap real native arithmetic with controlled termination status and optional run failure."""
        self.native, self.status, self.fail = native, status, fail

    def __getattr__(self, name):
        """Delegate unmodified operations to the actual HiGHS model."""
        return getattr(self.native, name)

    def run(self):
        """Execute real arithmetic unless injecting a native run error, then report the controlled stop."""
        if self.fail:
            return highspy.HighsStatus.kError
        self.native.run()
        return highspy.HighsStatus.kWarning

    def getModelStatus(self):
        """Return the injected native termination status independently of candidate arithmetic."""
        return self.status


class ControlledFactory:
    """Build real device mathematics while substituting controlled native stop reporting."""

    def __init__(self, **options):
        """Retain only the explicit native status/failure controls for this fixture."""
        self.options = options

    def build(self, problem):
        """Compose a real energy model and wrap its native object with the requested stop controls."""
        model = InstallationModelFactory().build(problem)
        model.context.native = NativeView(model.context.native, **self.options)
        return model


def test_feasible_stopped_candidate_is_returned_without_optimal_claim():
    """Report a controlled time stop after real solving. Retain the valid -0.28 candidate but mark it merely feasible."""
    case = CORE[0]
    result = HighsSolver(factory=ControlledFactory()).solve(case.problem)
    assert result.status == Status.FEASIBLE
    assert result.stop_reason == "time_limit"
    assert validate(case.problem, result.plan).objective == pytest.approx(float(case.objective))


def test_tolerance_optimal_result_retains_independent_native_bound(monkeypatch):
    """A -0.280 incumbent with a -0.281 bound is within 0.5 percent. Keep the bound instead of claiming exactness."""

    def bounded_info(view):
        """Report an explicit nonzero optimality gap over a real feasible candidate."""
        native_info = view.native.getInfo()
        return SimpleNamespace(
            primal_solution_status=native_info.primal_solution_status,
            mip_node_count=1,
            mip_dual_bound=-0.281,
        )

    monkeypatch.setattr(NativeView, "getInfo", bounded_info, raising=False)
    result = HighsSolver(factory=ControlledFactory(status=MS.kOptimal)).solve(CORE[0].problem)
    assert result.status == Status.OPTIMAL
    assert result.best_bound == -0.281


def test_native_run_error_does_not_return_a_candidate():
    """Inject native execution failure. Require error status with no usable plan."""
    result = HighsSolver(factory=ControlledFactory(fail=True)).solve(CORE[0].problem)
    assert result.status == Status.ERROR
    assert result.stop_reason == "native_run_error"
    assert result.plan is None


def test_extraction_failure_does_not_return_partial_results():
    """Fail candidate conversion after solving. Require adapter error without leaking a partial result."""

    class BrokenFactory:
        """Build real installation mathematics and fail its current extract method."""

        def build(self, problem):
            """Inject conversion failure only after ordinary construction and native solving."""
            model = InstallationModelFactory().build(problem)

            def fail():
                """Reject conversion before any partial result can be returned."""
                raise ValueError("broken result conversion")

            model.extract = fail
            return model

    result = HighsSolver(factory=BrokenFactory()).solve(CORE[0].problem)
    assert result.status == Status.ERROR
    assert result.plan is None
    assert result.stop_reason == "adapter_error:ValueError"


def test_real_unbounded_native_model_is_not_reported_as_profitable_plan():
    """Solve an unbounded negative-cost variable. Report unbounded/no plan instead of fictitious unlimited profit."""

    class UnboundedFactory:
        """Isolate native unbounded termination independently of installation constraints."""

        def build(self, problem):
            """Return a native negative-cost ray with no finite optimum."""
            native = highspy.Highs()
            native.setOptionValue("output_flag", False)
            native.addVariable(obj=-1)
            return SimpleNamespace(context=SimpleNamespace(native=native))

    result = HighsSolver(factory=UnboundedFactory()).solve(CORE[0].problem)
    assert result.status == Status.UNBOUNDED
    assert result.plan is None


def test_build_failure_has_no_partial_candidate():
    """Raise during model composition. Require adapter error without a partially built plan."""

    class BrokenFactory:
        """Fail construction before allocating a usable candidate."""

        def build(self, problem):
            """Inject a composition error that cannot produce a plan."""
            raise ValueError("incompatible components")

    result = HighsSolver(factory=BrokenFactory()).solve(CORE[0].problem)
    assert result.status == Status.ERROR
    assert result.plan is None
    assert result.stop_reason == "adapter_error:ValueError"


def test_reusing_the_solver_does_not_carry_state_between_planning_rounds():
    """Alternate arbitrage, terminal-stock and paid-charging cases. Require their original independent economic bounds each time."""
    solver = HighsSolver()
    for case in (CORE[0], CORE[2], CORE[1], CORE[0]):
        result = solver.solve(case.problem)
        assert result.status == Status.OPTIMAL
        assert validate(case.problem, result.plan).objective == pytest.approx(float(case.objective))
