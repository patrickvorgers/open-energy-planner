"""Small replaceable interface. No modeling-library types escape it."""

from typing import Protocol

from open_energy_planner.installation.model import InstallationProblem
from open_energy_planner.optimization.domain import SolveOptions, SolveResult


class Solver(Protocol):
    """Define a replaceable numerical solver using only domain input/output values."""

    def solve(
        self,
        problem: InstallationProblem,
        options: SolveOptions | None = None,
    ) -> SolveResult:
        """Solve an immutable problem and report feasibility, termination and any candidate."""
        ...
