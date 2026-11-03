"""The selected solver is mandatory. Missing dependencies must not skip tests."""

import pytest

from open_energy_planner.optimization.adapters.highs import HighsSolver


@pytest.fixture
def solver():
    """Provide the selected real HiGHS adapter so scenario assertions test production mathematics."""
    return HighsSolver()
