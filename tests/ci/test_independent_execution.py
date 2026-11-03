"""Prove selected suites reach real command boundaries and completion without implicit expansion."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from script.ci import check_images, check_suite_completion
from script.ci.ci_scope import selected_suites

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "profile,expected_services,supervisor",
    [
        ("selected:ci,unit-supervisor", {"native"}, False),
        ("selected:ci,supervisor,unit-supervisor", {"native"}, True),
        ("selected:ci,databases,unit-app", {"native", "db-tests"}, False),
        ("selected:ci,database-contracts", {"native", "db-tests"}, False),
        ("selected:ci,database-migrations", {"native", "db-tests"}, False),
        ("selected:ci,database-contracts,database-migrations", {"native", "db-tests"}, False),
        ("selected:ci,ha-controls", {"native", "ha-tests"}, False),
        ("selected:ci,ha", {"native", "ha-tests", "provision-tests"}, False),
        ("selected:ci,ha-solver", {"native", "ha-tests"}, False),
        ("selected:ci,unit-app-demand", {"native"}, False),
        ("selected:browser,ci,unit-frontend", {"native", "ui-tests"}, False),
        ("selected:ci,provisioning,unit-provisioning", {"native", "provision-tests"}, False),
    ],
)
def test_independent_selection_executes_exact_suites_and_reports_them(
    monkeypatch, tmp_path, profile, expected_services, supervisor
):
    """Supervisor-only selection cannot start unrelated HA/DB/browser tests or claim missing suite execution."""
    calls = []
    completed = []
    monkeypatch.setattr(subprocess, "run", lambda command, **options: calls.append(command))
    monkeypatch.setattr(check_images, "check_images", lambda *args: None)
    monkeypatch.setattr(check_suite_completion, "REPORT", tmp_path / "completion.json")
    monkeypatch.setattr(
        check_suite_completion,
        "write_completion",
        lambda route, suites: completed.append((route, suites.copy())),
    )
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", profile])
    runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    services = {
        command[command.index("--rm") + 1]
        for command in calls
        if "run" in command and "--rm" in command
    }
    assert services == expected_services
    if "unit-app-demand" in selected_suites(profile):
        smoke = next(
            index for index, command in enumerate(calls) if command[-1] == "script/app/smoke_app.py"
        )
        domain = next(
            index for index, command in enumerate(calls) if command[-1] == "unit-app-demand"
        )
        assert smoke < domain
    assert completed == [(profile, check_suite_completion.required_suites(profile))]
    assert (
        any(command[-1] == "script.supervisor.check_supervisor" for command in calls) is supervisor
    )
    assert any("postgres-test" in command for command in calls) is (
        bool({"databases", "database-contracts", "database-migrations"} & selected_suites(profile))
    )
    if not {"ha", "ha-controls", "ha-solver", "provisioning"} & selected_suites(profile):
        assert not any("up" in command and command[-1] == "app" for command in calls)


@pytest.mark.parametrize("failure", ["ci", "unit-supervisor", "supervisor"])
def test_failed_selected_suite_cannot_publish_completion(monkeypatch, tmp_path, failure):
    """Cheap failures block expensive acceptance. Any failure leaves no successful completion record."""
    calls = []
    completed = []
    report = tmp_path / "completion.json"
    report.write_text("stale success", encoding="utf-8")

    def run(command, **options):
        """Fail the chosen real subprocess boundary without starting a container."""
        calls.append(command)
        if (
            failure == "ci"
            and command[-1] == "--ci-only"
            or failure == "unit-supervisor"
            and command[-1] == "unit-supervisor"
            or failure == "supervisor"
            and command[-1] == "script.supervisor.check_supervisor"
        ):
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(check_images, "check_images", lambda *args: None)
    monkeypatch.setattr(check_suite_completion, "REPORT", report)
    monkeypatch.setattr(
        check_suite_completion, "write_completion", lambda *args: completed.append(args)
    )
    monkeypatch.setattr(
        sys, "argv", ["ci.py", "--profile", "selected:ci,supervisor,unit-supervisor"]
    )
    with pytest.raises(subprocess.CalledProcessError):
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    assert not report.exists()
    assert not completed
    assert any("down" in command for command in calls)
    if failure != "supervisor":
        assert not any(command[-1] == "script.supervisor.check_supervisor" for command in calls)


def test_completion_rejects_an_omitted_independent_suite(tmp_path):
    """Matching profile/tree cannot rescue a report that omitted selected acceptance."""
    report = tmp_path / "completion.json"
    for name in ("ci.xml", "supervisor-replay.xml", "unit-supervisor.xml"):
        (tmp_path / name).write_text(
            '<testsuite><testcase name="executed" /></testsuite>', encoding="utf-8"
        )
    document = {
        "profile": "selected:ci,supervisor,unit-supervisor",
        "source_tree": "a" * 40,
        "completed": ["ci", "supervisor", "unit-supervisor"],
        "executed_tests": {"ci": 1, "supervisor": 1, "unit-supervisor": 1},
    }
    report.write_text(json.dumps(document), encoding="utf-8")
    check_suite_completion.verify_completion(document["profile"], "a" * 40, report)
    document["completed"].remove("supervisor")
    report.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not proven"):
        check_suite_completion.verify_completion(
            "selected:ci,supervisor,unit-supervisor", "a" * 40, report
        )


def test_new_invocation_removes_stale_selected_junit_before_any_process(monkeypatch, tmp_path):
    """Old XML cannot make an unexecuted suite appear complete after even an early provenance failure."""
    report = tmp_path / "suite-completion.json"
    monkeypatch.setattr(check_suite_completion, "REPORT", report)
    selected = check_suite_completion.junit_reports("selected:ci,unit-supervisor")
    for reports in selected.values():
        for junit in reports:
            junit.write_text("old execution", encoding="utf-8")
    unrelated = tmp_path / "historical-browser.xml"
    unrelated.write_text("retained historical evidence", encoding="utf-8")

    def fail(command, **options):
        """The first subprocess must still be provenance, after clearing selected evidence."""
        assert command[-1] == "script/ci/check_provenance.py"
        assert all(not junit.exists() for reports in selected.values() for junit in reports)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", "selected:ci,unit-supervisor"])
    with pytest.raises(subprocess.CalledProcessError):
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    assert unrelated.read_text(encoding="utf-8") == "retained historical evidence"


def test_timing_report_failure_cannot_skip_scoped_cleanup(monkeypatch):
    """A diagnostic write failure still attempts container logs and removes this invocation's services."""
    calls = []
    original_write = Path.write_text

    def write(path, *arguments, **options):
        """Fail only the timing artifact, leaving other file and provenance behavior intact."""
        if path.name == "phase-times.json":
            raise OSError("No diagnostic disk space")
        return original_write(path, *arguments, **options)

    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(subprocess, "run", lambda command, **options: calls.append(command))
    monkeypatch.setattr(check_images, "check_images", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", "selected:ci,unit-supervisor"])
    with pytest.raises(OSError, match="diagnostic"):
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    assert calls[-2][-2:] == ["logs", "--no-color"]
    assert calls[-1][-3:] == ["down", "--volumes", "--remove-orphans"]


@pytest.mark.parametrize(
    "suite,service,directory",
    [
        ("ha-history", "ha-tests", "tests/custom_component/integration/history"),
        ("ha-solver", "ha-tests", "tests/custom_component/integration/controls/remote"),
        ("unit-app-demand", "native", "tests/app/demand"),
    ],
)
def test_packaged_preflight_uses_only_selected_group(suite, service, directory):
    """A focused candidate collects exactly its selected test directory in the correct environment."""
    profile = "selected:ci," + suite
    checks = list(check_images.image_checks(profile, selected_suites(profile)))
    assert [name for name, _, _ in checks] == ["ci", suite]
    assert checks[-1][1] == service
    assert directory in checks[-1][2]


def test_full_preflight_matches_disjoint_native_and_ha_execution():
    """Full collection checks early HA once and excludes the same repeated directories as execution."""
    checks = {
        suite: arguments
        for suite, _, arguments in check_images.image_checks("full", selected_suites("full"))
    }
    assert "--ignore=tests/database" in checks["native"]
    assert "tests/custom_component/integration/contracts" in checks["ha-contracts"]
    for worker in (0, 1):
        assert "--ignore=tests/custom_component/integration/contracts" in checks[f"ha-{worker}"]


def test_native_image_change_runs_every_shared_image_consumer(monkeypatch, tmp_path):
    """The native image covers all unit environments, real backends and Core provisioning without unrelated images."""
    from pathlib import PurePosixPath

    from script.ci.ci_scope import add_performance_queries, path_profile

    profile = add_performance_queries(path_profile(PurePosixPath("Dockerfile.native-test")))
    expected = {
        "ci",
        "unit-app",
        "unit-ha-contract",
        "unit-component",
        "unit-frontend",
        "unit-supervisor",
        "unit-provisioning",
        "unit-test-environment",
        "unit-release",
        "databases",
        "performance-queries",
        "provisioning",
    }
    assert set(selected_suites(profile)) == expected
    calls = []
    completed = []
    monkeypatch.setattr(subprocess, "run", lambda command, **options: calls.append(command))
    monkeypatch.setattr(check_images, "check_images", lambda *args: None)
    monkeypatch.setattr(check_suite_completion, "REPORT", tmp_path / "completion.json")
    monkeypatch.setattr(
        check_suite_completion,
        "write_completion",
        lambda route, suites: completed.append((route, suites.copy())),
    )
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", profile])
    runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    services = {
        command[command.index("--rm") + 1]
        for command in calls
        if "run" in command and "--rm" in command
    }
    assert services == {"native", "db-tests", "provision-tests"}
    assert {
        command[-1]
        for command in calls
        if "script.ci.pytest_suites" in command and command[-1].startswith("unit-")
    } == expected - {"ci", "databases", "performance-queries", "provisioning"}
    assert any(command[-1] == "script/app/smoke_app.py" for command in calls)
    assert any(command[-1] == "script/app/debug_scenario.py" for command in calls)
    smoke_index = next(
        index for index, command in enumerate(calls) if command[-1] == "script/app/smoke_app.py"
    )
    unit_index = next(index for index, command in enumerate(calls) if command[-1] == "unit-app")
    assert smoke_index < unit_index
    assert not any(command[-1] == "script.supervisor.check_supervisor" for command in calls)
    assert completed == [(profile, expected)]


def test_native_image_automatically_inherits_future_native_groups(monkeypatch):
    """A newly declared native group joins shared-image validation while a new HA group does not."""
    from pathlib import PurePosixPath

    from script.ci import ci_groups, ci_scope

    monkeypatch.setitem(ci_scope.PYTEST_AREAS, "unit-future", ("tests/future/unit",))
    monkeypatch.setitem(ci_scope.PYTEST_AREAS, "ha-future", ("tests/future/integration",))
    monkeypatch.setattr(
        ci_groups, "ATOMIC_SUITES", ci_groups.ATOMIC_SUITES | {"unit-future", "ha-future"}
    )
    actual = ci_scope.selected_suites(
        ci_scope.path_profile(PurePosixPath("Dockerfile.native-test"))
    )
    assert "unit-future" in actual
    assert "ha-future" not in actual


def test_unit_group_launcher_propagates_source_path_to_nested_python(tmp_path):
    """A lightweight fixture proves focused suites expose their source tree to a clean child interpreter."""
    import os
    from shutil import copyfile
    from xml.etree import ElementTree

    project = tmp_path / "project"
    scripts = project / "script/ci"
    scripts.mkdir(parents=True)
    for package in (scripts.parent, scripts):
        (package / "__init__.py").write_text(
            '"""Provide isolated runner fixtures."""\n', encoding="utf-8"
        )
    for name in ("ci_groups.py", "ha_suites.py", "pytest_suites.py"):
        copyfile(ROOT / "script/ci" / name, scripts / name)
    application = project / "src/open_energy_planner"
    application.mkdir(parents=True)
    (application / "__init__.py").write_text(
        '"""Expose an independent marker from the selected source tree."""\n'
        'SOURCE_MARKER = "isolated-source-marker"\n',
        encoding="utf-8",
    )
    tests = project / "tests/app"
    tests.mkdir(parents=True)
    (tests / "test_nested_source.py").write_text(
        '"""Exercise one clean child process without app dependencies."""\n'
        "import subprocess\nimport sys\n\n"
        "def test_child_import_uses_source_environment():\n"
        '    """Require the child interpreter to import the selected source tree."""\n'
        '    result = subprocess.run([sys.executable, "-c", '
        "'import open_energy_planner; assert open_energy_planner.SOURCE_MARKER == \"isolated-source-marker\"'], "
        "capture_output=True, text=True, check=False)\n"
        "    assert result.returncode == 0, result.stderr\n",
        encoding="utf-8",
    )
    report = tmp_path / "nested-python.xml"
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "script.ci.pytest_suites",
            "unit-app",
            "--junitxml=" + str(report),
        ],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cases = ElementTree.parse(report).findall(".//testcase")
    assert len(cases) == 1
    assert cases[0].get("name") == "test_child_import_uses_source_environment"
    assert all(cases[0].find(outcome) is None for outcome in ("failure", "error", "skipped"))


def test_focused_ha_executes_workers_contracts_and_provisioning_once(monkeypatch, tmp_path):
    """HA harness changes exercise their real worker entrypoint without unrelated heavy services."""
    calls = []
    completed = []
    monkeypatch.setattr(subprocess, "run", lambda command, **options: calls.append(command))
    monkeypatch.setattr(check_images, "check_images", lambda *args: None)
    monkeypatch.setattr(check_suite_completion, "REPORT", tmp_path / "completion.json")
    monkeypatch.setattr(
        check_suite_completion, "write_completion", lambda profile, suites: completed.append(suites)
    )
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", "selected:ci,ha"])
    runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    executions = [command for command in calls if "run" in command and "--rm" in command]
    assert len(executions) == 4
    assert sum(command[-1] == "ha-contracts" for command in executions) == 1
    workers = [command for command in executions if "script.ci.check_ha" in command]
    assert len(workers) == 1
    assert workers[0][-1] == "--ignore=tests/custom_component/integration/contracts"
    assert sum(command[-1] == "provision-tests" for command in executions) == 1
    assert completed == [{"ci", "ha-contracts", "ha", "provisioning"}]
    assert not any(
        "postgres-test" in command or "script.supervisor.check_supervisor" in command
        for command in calls
    )
    preflight = list(check_images.image_checks("selected:ci,ha", selected_suites("selected:ci,ha")))
    assert [name for name, _, _ in preflight] == [
        "ci",
        "ha-contracts",
        "ha-0",
        "ha-1",
        "provisioning-imports",
    ]
