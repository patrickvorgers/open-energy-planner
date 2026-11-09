"""Keep browser teaching-data reuse independent from mutable plans and active stores."""

from copy import deepcopy

import pytest

from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.frontend.browser import documentation_fixture


def test_teaching_example_is_solved_once_but_stores_and_plan_copies_are_independent(
    tmp_path, monkeypatch
):
    """One real validated solve seeds two fresh databases without sharing mutations or archive identity writes."""
    documentation_fixture._validated_example.cache_clear()
    original_solve = documentation_fixture.HighsSolver.solve
    solves = []
    stores = []

    def solve(solver, problem, options):
        """Count actual solves while retaining the production solver and independent validation."""
        solves.append(problem)
        return original_solve(solver, problem, options)

    monkeypatch.setattr(documentation_fixture.HighsSolver, "solve", solve)

    def store(directory):
        """Retain the actual seed objects so sharing a mutable cached document becomes observable."""
        repository = SqlPlanRepository(directory)
        stores.append(repository)
        return repository

    monkeypatch.setattr(documentation_fixture, "SqlPlanRepository", store)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    try:
        documentation_fixture.seed(first)
        with_first = SqlPlanRepository(first)
        try:
            original = deepcopy(with_first.state["plan"])
            stores[0].state["plan"]["comparison_names"]["roof"] = "Changed seed object"
            with_first.state["plan"]["comparison_names"]["roof"] = "Changed first copy"
            documentation_fixture.seed(second)
            with_second = SqlPlanRepository(second)
            try:
                assert with_second.state["plan"] == original
                with_second.state["fixture_isolation"] = "Second store only"
                with_second.save()
            finally:
                with_second.close()
            assert "fixture_isolation" not in with_first.state
            with pytest.raises(ValueError, match="Archived plan identity is immutable"):
                with_first.save()
        finally:
            with_first.close()
        assert len(solves) == 1
        assert documentation_fixture._validated_example()[0]["comparison_names"]["roof"] == (
            "Example south roof"
        )
    finally:
        documentation_fixture._validated_example.cache_clear()
