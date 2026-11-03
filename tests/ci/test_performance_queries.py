"""Keep volume checks separately selected, actually executed and independently evidenced."""

import runpy
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from script.ci import check_images, check_suite_completion
from script.ci.ci_scope import path_profile, select_profile, selected_suites

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "profile", ["documentation", "ci", "native", "frontend", "ha", "storage", "integration", "full"]
)
def test_original_matrix_never_implicitly_selects_query_performance(profile):
    """Matrix breadth alone never adds the extracted volume category."""
    assert "performance-queries" not in selected_suites(profile)


@pytest.mark.parametrize("query_changed", [False, True])
def test_full_composition_adds_performance_only_for_an_identified_query_change(
    monkeypatch, query_changed
):
    """A full composition change stays functional unless the same diff also changes a query owner."""
    paths = ["src/open_energy_planner/planning.py"]
    if query_changed:
        paths.append("src/open_energy_planner/prices/storage/adapters/future_query.py")
    records = "".join(
        f":100644 100644 {'3' * 40} {'4' * 40} M\0{path}\0" for path in paths
    ).encode()
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: records
    )
    assert select_profile("1" * 40, "2" * 40) == (
        "full+performance-queries" if query_changed else "full"
    )


@pytest.mark.parametrize(
    "failure", ["unknown", "unknown_storage", "invalid_revision", "unreadable_fixture"]
)
def test_fail_safe_is_full_without_automatic_performance(monkeypatch, failure):
    """Unknown ownership or unreadable comparison evidence retains full functional checks without volume seeds."""
    path = (
        "tests/database/support/broken.py"
        if failure == "unreadable_fixture"
        else "unknown/path.bin"
    )
    if failure == "unknown_storage":
        path = "src/open_energy_planner/new_domain/storage.py"
    record = f":100644 100644 {'3' * 40} {'4' * 40} M\0{path}\0".encode()
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: record
    )

    def unavailable_sources(*args):
        """Model malformed immutable fixture evidence rather than pretending the helper has no consumers."""
        raise SyntaxError("Unreadable fixture")

    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", unavailable_sources)
    base = "unavailable" if failure == "invalid_revision" else "1" * 40
    assert select_profile(base, "2" * 40) == "full"


@pytest.mark.parametrize("status", ["A", "M", "D"])
@pytest.mark.parametrize(
    "path",
    [
        "src/open_energy_planner/storage/adapters/documents.py",
        "src/open_energy_planner/storage/adapters/lifecycle/schema.py",
        "src/open_energy_planner/prices/storage/adapters/selection_store.py",
        "src/open_energy_planner/savings/storage/adapters/sqlalchemy.py",
        "tests/database/performance/test_future.py",
        "tests/database/support/history_datasets.py",
        "tests/database/support/settlement.py",
        "tests/database/conftest.py",
        "tests/database/support/backends.py",
        "tests/database/support/future_fixture.py",
        "pyproject.toml",
        "compose.yaml",
        "Dockerfile.native-test",
    ],
)
def test_complete_diff_cannot_drop_performance_owners(monkeypatch, path, status):
    """Query/schema, backend and fixture additions, modifications and deletions retain real volume checks."""
    old_mode = "000000" if status == "A" else "100644"
    new_mode = "000000" if status == "D" else "100644"
    record = f":{old_mode} {new_mode} {'3' * 40} {'4' * 40} {status}\0{path}\0".encode()
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: record
    )
    sources = {path: ""}
    if path.startswith("tests/database/") and "/performance/" not in path:
        sources["tests/database/contracts/test_consumer.py"] = "import " + path.removesuffix(
            ".py"
        ).replace("/", ".")
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: sources)
    profile = select_profile("1" * 40, "2" * 40)
    assert "performance-queries" in selected_suites(profile)
    if path.startswith("tests/database/"):
        expected = {"ci", "performance-queries"}
        if "/performance/" not in path:
            expected.add("databases")
        assert selected_suites(profile) == expected


@pytest.mark.parametrize(
    "path",
    [
        "src/open_energy_planner/app.py",
        "src/open_energy_planner/planning_policy.py",
        "src/open_energy_planner/web/public/style.css",
        "src/open_energy_planner/core/units.py",
        "tests/database/contracts/test_selection_store_backends.py",
    ],
)
def test_known_functional_changes_do_not_imply_volume_checks(path):
    """Known composition, UI, calculation and functional contracts keep their functional coverage without bulk seeds."""
    suites = selected_suites(path_profile(PurePosixPath(path)))
    assert "performance-queries" not in suites
    if path.endswith("app.py"):
        assert suites == {
            "ci",
            "browser",
            "databases",
            "ha-connection",
            "ha-contracts",
            "provisioning",
            "supervisor",
            "unit-app",
            "unit-ha-contract",
            "unit-supervisor",
        }


@pytest.mark.parametrize("status", ["A", "M", "D"])
@pytest.mark.parametrize("root_file", ["future_composition.py", "future_metadata.json"])
def test_undeclared_root_files_remain_full_for_every_change_status(monkeypatch, status, root_file):
    """Unknown flat owners retain full functional coverage for additions, changes and removals."""
    old_mode = "000000" if status == "A" else "100644"
    new_mode = "000000" if status == "D" else "100644"
    record = (
        f":{old_mode} {new_mode} {'3' * 40} {'4' * 40} {status}\0src/open_energy_planner/{root_file}\0"
    ).encode()
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: record
    )
    expected = "full"
    assert select_profile("1" * 40, "2" * 40) == expected


@pytest.mark.parametrize("failed", [False, True])
def test_performance_only_execution_uses_four_backends_and_requires_its_completion(
    monkeypatch, tmp_path, failed
):
    """A selected volume group starts dedicated databases and propagates failure without starting HA or Supervisor."""
    calls = []
    completed = []

    def run(command, **kwargs):
        """Observe real command arguments and optionally fail exactly the volume runner."""
        calls.append(command)
        if failed and command[-1] == "performance-queries":
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(check_images, "check_images", lambda *args: None)
    monkeypatch.setattr(check_suite_completion, "REPORT", tmp_path / "completion.json")
    monkeypatch.setattr(
        check_suite_completion,
        "write_completion",
        lambda profile, suites: completed.append(suites.copy()),
    )
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", "selected:ci,performance-queries"])
    if failed:
        with pytest.raises(subprocess.CalledProcessError):
            runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
        assert not completed
    else:
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
        assert completed == [{"ci", "performance-queries"}]
    assert any(command[-3:] == ["postgres-test", "mariadb-test", "mysql-test"] for command in calls)
    assert any(
        command[-3:] == ["-m", "script.ci.pytest_suites", "performance-queries"]
        for command in calls
    )
    assert not any(
        "ha-tests" in command
        or "ui-tests" in command
        or "script.supervisor.check_supervisor" in command
        for command in calls
    )
    assert calls[-1][-3:] == ["down", "--volumes", "--remove-orphans"]
