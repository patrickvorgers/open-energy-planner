"""Keep independent database groups compatible with legacy full functional coverage."""

import runpy
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from script.ci import check_suite_completion as completion
from script.ci.check_suite_completion import required_suites
from script.ci.ci_scope import path_profile, select_profile, selected_suites
from script.ci.pytest_suites import pytest_arguments


def test_legacy_database_parent_still_collects_every_functional_group():
    """An older immutable policy selecting databases must retain transfers and ordinary correctness."""
    arguments = pytest_arguments("databases")
    assert "tests/database/contracts" in arguments
    assert "tests/database/migrations" in arguments
    assert "databases" in required_suites("full")


@pytest.mark.parametrize(
    "group,directory",
    [
        ("database-contracts", "contracts"),
        ("database-migrations", "migrations"),
    ],
)
def test_new_functional_database_groups_have_independent_execution_and_reports(group, directory):
    """A selected child owns exactly its directory and fresh JUnit completion evidence."""
    arguments = pytest_arguments(group)
    assert set(arguments) & {
        "tests/database/contracts",
        "tests/database/migrations",
    } == {f"tests/database/{directory}"}
    assert f"--junitxml=results/ci/{group}.xml" in arguments
    assert required_suites("selected:ci," + group) == {"ci", group}
    assert selected_suites(
        path_profile(PurePosixPath(f"tests/database/{directory}/test_future.py"))
    ) == {"ci", group}


def test_transfer_scenarios_are_outside_the_ordinary_contract_group():
    """Collection ownership follows actual transfers, including the settlement scenarios."""
    root = Path(__file__).resolve().parents[2]
    assert (root / "tests/database/migrations/test_archive_migration.py").is_file()
    assert (root / "tests/database/migrations/test_savings_transfer.py").is_file()
    assert (root / "tests/database/contracts/test_archive_lifecycle.py").is_file()


def test_read_only_query_owner_omits_backend_transfer_but_keeps_correctness():
    """A query projection cannot force every cross-backend transfer by living under storage."""
    suites = selected_suites(
        path_profile(
            PurePosixPath("src/open_energy_planner/prices/storage/adapters/queries/future.py")
        )
    )
    assert {"unit-app", "browser", "database-contracts"} <= suites
    assert not {"databases", "database-migrations"} & suites


def test_storage_writers_and_shared_fixtures_keep_all_functional_databases():
    """Unreviewed write or fixture changes retain migration coverage through the legacy parent."""
    for source_path in (
        "src/open_energy_planner/storage/adapters/documents.py",
        "src/open_energy_planner/contracts/wire.py",
        "src/open_energy_planner/solar/model.py",
        "src/open_energy_planner/storage/adapters/lifecycle/migration.py",
        "tests/database/support/future.py",
        "tests/database/conftest.py",
    ):
        assert "databases" in selected_suites(path_profile(PurePosixPath(source_path)))


@pytest.mark.parametrize("evidence", ["missing", "empty", "skipped", "failed"])
def test_migration_selection_requires_fresh_executed_cases(tmp_path, monkeypatch, evidence):
    """A passing ordinary database report cannot substitute for a selected transfer group's execution."""
    monkeypatch.setattr(completion, "REPORT", tmp_path / "completion.json")
    for report_name in ("ci.xml", "database-contracts.xml"):
        (tmp_path / report_name).write_text(
            '<testsuite><testcase name="passed" /></testsuite>', encoding="utf-8"
        )
    if evidence != "missing":
        (tmp_path / "database-migrations.xml").write_text(
            {
                "empty": "<testsuite />",
                "skipped": '<testsuite><testcase name="transfer"><skipped /></testcase></testsuite>',
                "failed": '<testsuite><testcase name="transfer"><failure /></testcase></testsuite>',
            }[evidence],
            encoding="utf-8",
        )
    with pytest.raises(RuntimeError):
        completion.write_completion(
            "selected:ci,database-contracts,database-migrations",
            {"ci", "database-contracts", "database-migrations"},
        )
    assert not completion.REPORT.exists()


@pytest.mark.parametrize("failure", ["database-contracts", "database-migrations"])
def test_failed_database_child_blocks_completion_and_cleans_up(tmp_path, monkeypatch, failure):
    """Failure in either sequential backend group reaches the controller before any success marker."""
    from script.ci import check_images

    calls = []
    completed = []

    def run(command, **options):
        """Fail the chosen real command boundary without starting databases."""
        calls.append(command)
        if command[-1] == failure:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(check_images, "check_images", lambda *args: None)
    monkeypatch.setattr(completion, "REPORT", tmp_path / "completion.json")
    monkeypatch.setattr(completion, "write_completion", lambda *args: completed.append(args))
    monkeypatch.setattr(
        sys, "argv", ["ci.py", "--profile", "selected:ci,database-contracts,database-migrations"]
    )
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(subprocess.CalledProcessError):
        runpy.run_path(str(root / "script/ci/ci.py"), run_name="__main__")
    assert not completed
    assert any("down" in command for command in calls)
    executed = [
        command[-1]
        for command in calls
        if command[-1] in {"database-contracts", "database-migrations"}
    ]
    assert executed == (
        ["database-contracts"]
        if failure == "database-contracts"
        else ["database-contracts", "database-migrations"]
    )


@pytest.mark.parametrize(
    "domain,ha_suites",
    [
        ("prices", set()),
        ("thermal", {"ha-thermal"}),
        ("installation", {"ha-contracts", "ha-controls", "ha-thermal", "ha-history"}),
    ],
)
def test_query_only_change_retains_independent_volume_checks(monkeypatch, domain, ha_suites):
    """Read-only storage omits transfers while keeping performance and its domain's HA contracts."""
    path = f"src/open_energy_planner/{domain}/storage/adapters/queries/future.py"
    record = f":100644 100644 {'3' * 40} {'4' * 40} M\0{path}\0".encode()
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: record
    )
    assert (
        selected_suites(select_profile("1" * 40, "2" * 40))
        == {
            "ci",
            "unit-app",
            "unit-ha-contract",
            "browser",
            "database-contracts",
            "performance-queries",
        }
        | ha_suites
    )
