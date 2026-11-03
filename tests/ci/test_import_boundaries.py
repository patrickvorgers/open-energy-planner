"""Keep standalone integration packaging and app/core responsibilities testable."""

import ast
from pathlib import Path, PurePosixPath

import pytest

from script.ci.ci_scope import path_profile, performance_queries_path, selected_suites

ROOT = Path(__file__).resolve().parents[2]
MEASUREMENT_FORBIDDEN = (
    "homeassistant",
    "aiohttp",
    "highspy",
    "sqlalchemy",
    "open_energy_planner",
    "measurement_sources",
    "custom_components.open_energy_planner.measurement_sources",
)


def modules(path):
    """Collect import targets from Python syntax for framework/adapter boundary assertions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (name.name for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module
            for name in node.names:
                yield f"{node.module}.{name.name}" if node.module else name.name


@pytest.mark.parametrize("name", ["calculation/model.py", "learning.py", "basis.py", "wire.py"])
def test_solar_contracts_do_not_import_physical_model_library(name):
    """Keep pvlib/pandas inside the predictor adapter. Solar contracts and learning stay library-neutral."""
    path = ROOT / "src/open_energy_planner/solar" / name
    assert not any(module_name.startswith(("pvlib", "pandas")) for module_name in modules(path))


def test_app_does_not_import_home_assistant_or_integration():
    """Inspect app imports. Reject dependencies on the HA framework or custom integration implementation."""
    for path in (ROOT / "src").rglob("*.py"):
        assert not any(
            module_name.startswith(("homeassistant", "custom_components"))
            for module_name in modules(path)
        )


def test_integration_does_not_import_app_solver_or_orm():
    """Inspect integration imports. Require the thin adapter to avoid app, solver and storage implementation packages."""
    for path in (ROOT / "custom_components/open_energy_planner").rglob("*.py"):
        assert not any(
            module_name.startswith(
                (
                    "open_energy_planner",
                    "oep_probe",
                    "highspy",
                    "sqlalchemy",
                    "pvlib",
                    "numpy",
                    "pandas",
                )
            )
            for module_name in modules(path)
        )


def test_wire_and_schedule_have_no_framework_dependencies():
    """Inspect wire/schedule imports. Require plain contracts independent of transport, HA and native optimization."""
    path = ROOT / "custom_components/open_energy_planner/protocol.py"
    assert not any(
        module_name.startswith(("homeassistant", "aiohttp", "highspy", "sqlalchemy"))
        for module_name in modules(path)
    )


def test_measurement_contract_has_no_transport_framework_or_solver_dependencies():
    """Inspect measurement contracts. Require normalized immutable data without adapter-library dependencies."""
    path = ROOT / "custom_components/open_energy_planner/measurements.py"
    assert not any(module_name.startswith(MEASUREMENT_FORBIDDEN) for module_name in modules(path))


@pytest.mark.parametrize("name", ["model.py", "service.py", "selection.py"])
def test_price_core_does_not_import_source_transport_or_storage_adapters(name):
    """Inspect price contracts/orchestration. Reject provider, HTTP, HA, ORM or solver dependencies crossing the adapter boundary."""
    path = ROOT / "src/open_energy_planner/prices" / name
    forbidden = (
        "aiohttp",
        "homeassistant",
        "sqlalchemy",
        "highspy",
        "adapters",
        "open_energy_planner.prices.adapters",
    )
    assert not any(module_name.startswith(forbidden) for module_name in modules(path))


def test_app_database_dependencies_are_confined_to_sqlalchemy_storage_adapters():
    """Active app modules cannot bypass SQLAlchemy or leak its native objects into source policy and calculations."""
    root = ROOT / "src/open_energy_planner"
    for path in root.rglob("*.py"):
        imports = tuple(modules(path))
        assert not any(
            module_name.startswith(("sqlite3", "psycopg", "pymysql", "mysql.connector"))
            for module_name in imports
        ), path
        if any(module_name.startswith("sqlalchemy") for module_name in imports):
            assert "adapters" in path.relative_to(root).parts, path
            area = path.relative_to(ROOT).as_posix()
            assert {"databases", "database-contracts"} & selected_suites(
                path_profile(PurePosixPath(area))
            ), path
            assert performance_queries_path(PurePosixPath(area)), path


def test_shared_solar_defaults_do_not_import_the_numerical_model_adapter():
    """Sharing physical defaults must not make app/HA bootstrap import pvlib or its numerical stack."""
    root = ROOT / "src/open_energy_planner/solar"
    assert not any(
        module_name.startswith(("numpy", "pandas", "pvlib", "predictor"))
        for module_name in modules(root / "model_policy.py")
    )
    tree = ast.parse((root / "runtime.py").read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "predictor" for node in tree.body
    )


@pytest.mark.parametrize(
    "name",
    [
        "contracts/calculation/formula.py",
        "contracts/model.py",
        "contracts/bonus.py",
        "contracts/compilation.py",
        "installation/location.py",
        "history/model.py",
        "contracts/bonus_history.py",
        "demand/calculation/model.py",
        "demand/reconstruction.py",
        "demand/calculation/forecasting.py",
        "demand/evaluation.py",
        "solar/calculation/model.py",
        "solar/learning.py",
        "solar/basis.py",
        "demand/basis.py",
        "contracts/planning_basis.py",
        "installation/model.py",
        "installation/validation.py",
        "installation/basis.py",
        "installation/commands.py",
    ],
)
def test_contract_calculation_has_no_platform_storage_or_native_solver_dependencies(name):
    """Keep formula evaluation and tariff assembly independent of provider payloads, persistence and native model builders."""
    path = ROOT / "src/open_energy_planner" / name
    forbidden = (
        "aiohttp",
        "homeassistant",
        "sqlalchemy",
        "highspy",
        "adapters",
        "contract_storage",
        "open_energy_planner.contracts.storage",
    )
    assert not any(module_name.startswith(forbidden) for module_name in modules(path))


@pytest.mark.parametrize(
    "statement",
    [
        "from .measurement_sources import StateNormalizer",
        "from custom_components.open_energy_planner.measurement_sources import StateNormalizer",
        "from . import measurement_sources",
        "from custom_components.open_energy_planner import measurement_sources",
    ],
)
def test_measurement_boundary_detects_absolute_relative_and_package_imports(tmp_path, statement):
    """Present different import spellings. Require boundary detection to catch each forbidden adapter dependency."""
    path = tmp_path / "measurement_contract.py"
    path.write_text(statement)
    assert any(module.startswith(MEASUREMENT_FORBIDDEN) for module in modules(path))
