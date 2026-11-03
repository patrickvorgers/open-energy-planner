"""Prove the remaining plan cases use their own contracts without unrelated performance or migrations."""

import subprocess
from pathlib import PurePosixPath

import pytest

from script.ci.ci_scope import (
    path_profile,
    performance_queries_path,
    select_profile,
    selected_suites,
)
from script.ci.ha_suites import ha_selection


@pytest.mark.parametrize(
    "path",
    [
        "script/ci/ci_scope.py",
        "script/ci/ci_groups.py",
        ".github/workflows/ci-admission.yaml",
    ],
)
@pytest.mark.parametrize("status", ["A", "M", "D"])
def test_pure_policy_and_admission_changes_select_only_ci(monkeypatch, path, status):
    """Every ordinary lifecycle edit of a reviewed metadata owner selects CI regressions alone."""
    before = "000000" if status == "A" else "100644"
    after = "000000" if status == "D" else "100644"
    record = f":{before} {after} {'3' * 40} {'4' * 40} {status}\0{path}\0".encode()
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: record)
    assert select_profile("1" * 40, "2" * 40) == "ci"
    assert not performance_queries_path(PurePosixPath(path))


@pytest.mark.parametrize("path", ["script/ci/ha_suites.py", "script/ci/ha_shard.py"])
def test_ha_configuration_keeps_all_ha_contracts_without_database_or_volume_groups(path):
    """HA argument and shard edits keep real HA acceptance while omitting backend transfers and benchmarks."""
    suites = selected_suites(path_profile(PurePosixPath(path)))
    assert suites == {"ci", "ha"}
    assert not performance_queries_path(PurePosixPath(path))


@pytest.mark.parametrize(
    "path",
    ["script/ci/pytest_suites.py", "script/ci/proof_git.py", ".github/workflows/checks.yaml"],
)
def test_shared_execution_and_snapshot_process_owners_retain_broad_coverage(path):
    """General process, shared pytest and heavy workflow edits cannot inherit metadata-only coverage."""
    assert path_profile(PurePosixPath(path)) == "full"
    assert performance_queries_path(PurePosixPath(path))


def test_ha_argument_owner_preserves_worker_plugin_directories_and_independent_report():
    """An HA worker still receives both integration roots and its own original report identity."""
    arguments, report = ha_selection("ha", 1)
    assert arguments == [
        "tests/custom_component/integration",
        "tests/test_environment/integration",
        "-p",
        "script.ci.ha_shard",
        "--ha-shard",
        "1",
    ]
    assert report == "ha-1"
    with pytest.raises(ValueError):
        ha_selection("ha")


@pytest.mark.parametrize("workload", ["query", "writer", "shared"])
def test_ha_policy_changes_never_remove_mixed_workload_coverage(monkeypatch, workload):
    """Independent HA ownership cannot suppress volume, real migration or shared execution proof."""
    paths = {
        "query": "src/open_energy_planner/prices/storage/adapters/queries/future.py",
        "writer": "src/open_energy_planner/storage/adapters/lifecycle/future.py",
        "shared": "script/ci/pytest_suites.py",
    }
    records = "".join(
        f":100644 100644 {'3' * 40} {'4' * 40} M\0{path}\0"
        for path in ("script/ci/ha_suites.py", paths[workload])
    ).encode()
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: records)
    suites = selected_suites(select_profile("1" * 40, "2" * 40))
    assert "performance-queries" in suites
    if workload == "query":
        assert {"database-contracts", "ha", "browser", "unit-app"} <= suites
        assert "databases" not in suites and "database-migrations" not in suites
    elif workload == "writer":
        assert {"databases", "ha", "supervisor"} <= suites
    else:
        assert suites == {
            "native",
            "browser",
            "ha",
            "databases",
            "supervisor",
            "performance-queries",
        }


def test_ha_group_directories_stay_inside_real_ha_roots():
    """Pure HA argument metadata cannot redirect an HA-only run to database or volume tests."""
    from script.ci.ci_groups import HA_TEST_DIRECTORIES, PYTEST_AREAS

    roots = ("tests/custom_component/integration", "tests/test_environment/integration")
    assert HA_TEST_DIRECTORIES == roots
    for suite, directories in PYTEST_AREAS.items():
        if suite.startswith("ha-"):
            assert directories
            assert all(
                not PurePosixPath(directory).is_absolute()
                and ".." not in PurePosixPath(directory).parts
                and "\\" not in directory
                for directory in directories
            )
            assert all(
                any(directory == root or directory.startswith(root + "/") for root in roots)
                for directory in directories
            )
