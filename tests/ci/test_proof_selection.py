"""Keep metadata-only changes cheap without losing mixed or unknown workload coverage."""

import runpy
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from script.ci import check_images, check_suite_completion
from script.ci.ci_scope import (
    path_profile,
    performance_queries_path,
    select_profile,
    selected_suites,
)

PROOF_FILES = (
    "label_ci_scope.py",
    "check_ci_candidate.py",
    "check_commit_signatures.py",
    "check_main_ci_reuse.py",
    "check_suite_completion.py",
    "check_provenance.py",
    "review_preflight.py",
    "scope_report.py",
    "ci_scope.py",
    "ci_groups.py",
)


@pytest.mark.parametrize("name", PROOF_FILES)
@pytest.mark.parametrize("status", ["A", "M", "D"])
def test_proof_only_diff_selects_ci_without_workloads(monkeypatch, name, status):
    """Each reviewed proof owner selects only CI regressions across its entire file lifecycle."""
    path = "script/ci/" + name
    old_mode = "000000" if status == "A" else "100644"
    new_mode = "000000" if status == "D" else "100644"
    record = f":{old_mode} {new_mode} {'3' * 40} {'4' * 40} {status}\0{path}\0".encode()
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: record)
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: {})
    assert select_profile("1" * 40, "2" * 40) == "ci"
    assert not performance_queries_path(PurePosixPath(path))


@pytest.mark.parametrize(
    "path",
    [
        "script/ci/proof_git.py",
        "script/ci/pytest_suites.py",
        "script/ci/ci.py",
        "script/ci/check_images.py",
        "script/ci/future.py",
    ],
)
def test_execution_policy_and_unknown_ci_owners_remain_conservative(path):
    """Changing execution capabilities or unknown harness code retains full and performance coverage."""
    assert path_profile(PurePosixPath(path)) == "full"
    assert performance_queries_path(PurePosixPath(path))


@pytest.mark.parametrize("path", [".coderabbit.yaml", ".github/workflows/review-preflight.yaml"])
@pytest.mark.parametrize("status", ["A", "M", "D"])
def test_review_configuration_has_no_product_or_query_workload(monkeypatch, path, status):
    """Declared review configuration retains CI proof across additions, edits and removals."""
    old_mode = "000000" if status == "A" else "100644"
    new_mode = "000000" if status == "D" else "100644"
    record = f":{old_mode} {new_mode} {'3' * 40} {'4' * 40} {status}\0{path}\0".encode()
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: record)
    assert select_profile("1" * 40, "2" * 40) == "ci"
    assert not performance_queries_path(PurePosixPath(path))


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/future-preflight.yaml",
        "script/ci/future_preflight.py",
        "compose.yaml",
        ".github/workflows/checks.yaml",
    ],
)
def test_review_metadata_does_not_capture_unknown_or_shared_execution(path):
    """Similar names and real workload owners cannot inherit the metadata-only route."""
    assert path_profile(PurePosixPath(path)) == "full"


@pytest.mark.parametrize(
    "metadata", [".coderabbit.yaml", ".github/workflows/review-preflight.yaml"]
)
@pytest.mark.parametrize(
    "workload",
    [".github/workflows/checks.yaml", "script/ci/pytest_suites.py", "compose.yaml"],
)
def test_review_metadata_cannot_remove_shared_workload_coverage(monkeypatch, metadata, workload):
    """Review-only changes cannot conceal execution or database-fixture changes in the same PR."""
    records = "".join(
        f":100644 100644 {'3' * 40} {'4' * 40} M\0{path}\0" for path in (metadata, workload)
    ).encode()
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: records)
    assert select_profile("1" * 40, "2" * 40) == "full+performance-queries"


@pytest.mark.parametrize(
    "path,expected",
    [
        (
            "src/open_energy_planner/prices/storage/adapters/queries/future.py",
            {
                "ci",
                "unit-app",
                "unit-ha-contract",
                "browser",
                "database-contracts",
                "performance-queries",
            },
        ),
        (
            "src/open_energy_planner/storage/adapters/lifecycle/future.py",
            {
                "ci",
                "unit-app",
                "databases",
                "performance-queries",
                "ha-contracts",
                "ha-connection",
                "provisioning",
                "supervisor",
                "unit-supervisor",
            },
        ),
        (
            "script/ci/future.py",
            {"native", "browser", "ha", "databases", "supervisor", "performance-queries"},
        ),
    ],
)
def test_mixed_proof_diff_keeps_every_workload_owner(monkeypatch, path, expected):
    """A cheap proof edit never removes contracts, transfers or volume proof required by another path."""
    records = "".join(
        f":100644 100644 {'3' * 40} {'4' * 40} M\0{changed}\0"
        for changed in ("script/ci/check_ci_candidate.py", path)
    ).encode()
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: records)
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: {})
    assert selected_suites(select_profile("1" * 40, "2" * 40)) == expected


def test_renaming_proof_into_unknown_owner_retains_full(monkeypatch):
    """Both sides of a move participate, so a former proof name cannot make an unknown owner cheap."""
    records = (
        f":100644 000000 {'3' * 40} {'0' * 40} D\0script/ci/check_ci_candidate.py\0"
        f":000000 100644 {'0' * 40} {'4' * 40} A\0script/ci/future.py\0"
    ).encode()
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: records)
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: {})
    assert select_profile("1" * 40, "2" * 40) == "full+performance-queries"


def test_ci_route_runs_only_native_regressions(monkeypatch, tmp_path):
    """The existing real orchestrator does not launch database, HA, app or Supervisor services for CI."""
    calls = []
    completed = []
    monkeypatch.setattr(subprocess, "run", lambda command, **options: calls.append(command))
    monkeypatch.setattr(check_images, "check_images", lambda *args: None)
    monkeypatch.setattr(check_suite_completion, "REPORT", tmp_path / "completion.json")
    monkeypatch.setattr(
        check_suite_completion,
        "write_completion",
        lambda profile, suites: completed.append((profile, suites.copy())),
    )
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", "ci"])
    root = Path(__file__).resolve().parents[2]
    runpy.run_path(str(root / "script/ci/ci.py"), run_name="__main__")
    launches = [command for command in calls if "run" in command and "--rm" in command]
    assert len(launches) == 1
    assert launches[0][-3:] == ["native", "script/ci/check_native.py", "--ci-only"]
    assert completed == [("ci", {"ci"})]


def test_ci_native_environment_has_no_workload_services_or_docker_socket():
    """The explicit native service contract retains no network, dependency services or privileged mounts."""
    root = Path(__file__).resolve().parents[2]
    source = (root / "compose.yaml").read_text(encoding="utf-8")
    native = source.split("  native:\n", 1)[1].split("\n  ha-tests:", 1)[0]
    # This is a structural execution policy guard, not app behavior inferred from source text.
    assert "    network_mode: none\n" in native
    assert "depends_on:" not in native
    assert "privileged:" not in native
    assert "docker.sock" not in native
    assert "      - ./results/ci:/workspace/results/ci\n" in native
