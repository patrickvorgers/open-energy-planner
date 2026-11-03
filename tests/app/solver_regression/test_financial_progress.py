"""Financial telemetry is bounded, honest and cannot replace or block independently checked plans."""

from time import perf_counter
from types import SimpleNamespace

import pytest

from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.adapters.highs.progress import FinancialProgress
from open_energy_planner.optimization.domain import SolveOptions, Status
from tests.app.solver_regression.cases import CORE


def test_progress_preserves_first_latest_and_real_zero_without_infinite_bounds():
    """Retain zero as a candidate and unknown bounds as null, compacting intermediate observations."""
    progress = FinancialProgress(perf_counter())
    progress.record(float("inf"), -1, 0)
    assert progress.samples == []
    progress.record(0, float("inf"), 1)
    for sample_index in range(1, 1000):
        progress.record(-sample_index / 1000, -1, sample_index + 1)
    assert len(progress.samples) <= 256
    assert progress.samples[0].objective == 0
    assert progress.samples[0].best_bound is None
    assert progress.samples[-1].objective == -0.999
    assert progress.first_candidate_seconds is None


def test_callback_faults_do_not_escape_and_final_fallback_is_not_a_first_candidate():
    """Broken native event payloads remain telemetry failures, never solver failures or fake timings."""
    progress = FinancialProgress(perf_counter())
    progress.observe(None)
    progress.record(-0.28, -0.28, 3)
    assert progress.first_candidate_seconds is None
    progress.observe(
        SimpleNamespace(data_out=SimpleNamespace(mip_primal_bound=-0.3, mip_dual_bound=-0.4))
    )
    assert progress.first_candidate_seconds is not None


@pytest.mark.parametrize("broken", [False, True])
def test_native_progress_leaves_independent_negative_cost_and_budget_unchanged(monkeypatch, broken):
    """Buying at 0.12 and selling at 0.40 retains the independent -0.28 result with broken telemetry."""
    if broken:
        monkeypatch.setattr(FinancialProgress, "observe", lambda *_: None)
    result = HighsSolver().solve(CORE[0].problem, SolveOptions(time_limit_s=7))
    assert result.status == Status.OPTIMAL
    assert validate(CORE[0].problem, result.plan).objective == pytest.approx(-0.28)
    assert result.time_limit_seconds == 7
    assert result.progress[-1].objective == pytest.approx(-0.28)
    assert result.financial_started_seconds >= result.build_seconds
    if broken:
        assert result.first_candidate_seconds is None


def test_broken_sample_collection_does_not_reject_native_candidate(monkeypatch):
    """Callback and terminal sample failures leave the real -0.28 candidate independently valid."""

    def fail_sample(*_arguments):
        """Inject a collector failure after native solving rather than changing the model."""
        raise ValueError("Simulated telemetry failure")

    monkeypatch.setattr(FinancialProgress, "record", fail_sample)
    result = HighsSolver().solve(CORE[0].problem)
    assert result.status == Status.OPTIMAL
    assert validate(CORE[0].problem, result.plan).objective == pytest.approx(-0.28)
    assert result.progress == ()
    assert result.first_candidate_seconds is None


@pytest.mark.parametrize("case", CORE, ids=lambda case: case.name)
def test_financial_progress_preserves_native_objective_units(case):
    """Hand-derived spread, negative prices and terminal-stock values retain the same currency objective."""
    result = HighsSolver().solve(case.problem)
    assert result.status == Status.OPTIMAL
    assert result.progress[-1].objective == pytest.approx(float(case.objective))
    assert validate(case.problem, result.plan).objective == pytest.approx(float(case.objective))
