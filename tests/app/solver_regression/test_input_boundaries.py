"""Reject ambiguous or non-finite optimization inputs before model construction."""

import ast
from dataclasses import replace
from datetime import timedelta
from importlib.util import resolve_name
from pathlib import Path

import pytest

from open_energy_planner.optimization.domain import Battery
from tests.app.solver_regression.cases import CORE


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_price_is_rejected(price):
    """Supply NaN/infinite prices. Reject them before undefined economics can enter optimization."""
    with pytest.raises(ValueError, match="Prices must be finite"):
        replace(CORE[0].problem.intervals[0], buy_price=price)


def test_missing_interval_cannot_silently_extend_energy_availability():
    """Create a gap in interval coverage. Reject it instead of carrying energy across an unmodelled period."""
    first, second = CORE[0].problem.intervals
    with pytest.raises(ValueError, match="contiguous"):
        replace(
            CORE[0].problem,
            intervals=(first, replace(second, start=second.start + timedelta(minutes=15))),
        )


def test_naive_timestamp_is_rejected():
    """Supply a timestamp without timezone. Reject ambiguous chronological input."""
    interval = CORE[0].problem.intervals[0]
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(interval, start=interval.start.replace(tzinfo=None))


def test_below_reserve_input_preserves_measured_energy_and_configured_minimum():
    """A measured 0.1 kWh stays 0.1 below a distinct 0.2 kWh reserve. Neither value is silently clamped."""
    battery = Battery(1, 0.1, minimum_kwh=0.2)
    assert battery.initial_kwh == 0.1
    assert battery.minimum_kwh == 0.2


def forbidden_imports(source, package):
    """Resolve absolute and relative import targets before checking independent-core boundaries."""
    forbidden = ("pyomo", "highspy", "open_energy_planner.optimization.adapters")
    imports = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            imports.extend((module, *(f"{module}.{alias.name}" for alias in node.names)))
    return tuple(module for module in imports if module.startswith(forbidden))


@pytest.mark.parametrize(
    ("source", "package"),
    [
        ("import highspy", "open_energy_planner.installation"),
        ("from highspy import Highs", "open_energy_planner.installation"),
        ("from .adapters.highs import HighsSolver", "open_energy_planner.optimization"),
        ("from ..optimization.adapters import highs", "open_energy_planner.installation"),
        ("from ..optimization import adapters", "open_energy_planner.installation"),
        ("from . import adapters", "open_energy_planner.optimization"),
    ],
)
def test_boundary_scan_detects_absolute_and_relative_solver_imports(source, package):
    """Reject explicit solver imports and parent-relative aliases that formerly bypassed the boundary scan."""
    assert forbidden_imports(source, package)


def test_boundary_scan_allows_current_plain_domain_imports():
    """Allow relative installation/domain contracts without flagging unrelated plain modules as adapters."""
    assert not forbidden_imports(
        "from ..optimization.domain import Battery", "open_energy_planner.installation"
    )


def test_domain_port_and_validator_do_not_import_solver_libraries():
    """Inspect core dependency boundaries. Require domain/validation code to remain independent of solver packages."""
    root = Path(__file__).resolve().parents[3] / "src" / "open_energy_planner"
    for relative in (
        "optimization/domain.py",
        "optimization/ports.py",
        "optimization/export_bonus.py",
        "installation/model.py",
        "installation/validation.py",
        "appliances/ev/validation.py",
        "appliances/thermal/validation.py",
        "appliances/cycles/model.py",
        "appliances/sources.py",
    ):
        package = "open_energy_planner." + ".".join(Path(relative).parts[:-1])
        assert not forbidden_imports((root / relative).read_text(encoding="utf-8"), package), (
            relative
        )
