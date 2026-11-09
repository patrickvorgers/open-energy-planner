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
    seed_repositories = []

    def solve(solver, problem, options):
        """Count actual solves while retaining the production solver and independent validation."""
        solves.append(problem)
        return original_solve(solver, problem, options)

    monkeypatch.setattr(documentation_fixture.HighsSolver, "solve", solve)

    def open_seed_repository(directory):
        """Retain the actual seed objects so sharing a mutable cached document becomes observable."""
        repository = SqlPlanRepository(directory)
        seed_repositories.append(repository)
        return repository

    monkeypatch.setattr(documentation_fixture, "SqlPlanRepository", open_seed_repository)
    first_directory, second_directory = tmp_path / "first", tmp_path / "second"
    first_directory.mkdir()
    second_directory.mkdir()
    try:
        documentation_fixture.seed(first_directory)
        first_repository = SqlPlanRepository(first_directory)
        try:
            original = deepcopy(first_repository.state["plan"])
            seed_repositories[0].state["plan"]["comparison_names"]["roof"] = "Changed seed object"
            first_repository.state["plan"]["comparison_names"]["roof"] = "Changed first copy"
            documentation_fixture.seed(second_directory)
            second_repository = SqlPlanRepository(second_directory)
            try:
                assert second_repository.state["plan"] == original
                second_repository.state["fixture_isolation"] = "Second store only"
                second_repository.save()
            finally:
                second_repository.close()
            assert "fixture_isolation" not in first_repository.state
            with pytest.raises(ValueError, match="Archived plan identity is immutable"):
                first_repository.save()
        finally:
            first_repository.close()
        assert len(solves) == 1
        assert documentation_fixture._validated_example()[0]["comparison_names"]["roof"] == (
            "Example south roof"
        )
    finally:
        documentation_fixture._validated_example.cache_clear()
