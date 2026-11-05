"""Keep focused CI conservative and preserve the full pipeline for every other change."""

import json
import re
import runpy
import subprocess
import sys
from pathlib import Path
from threading import Event, Lock

import pytest

from script.ci import check_images, check_suite_completion
from script.ci.ci_scope import documentation_only, main, matrix_profile, select_profile

ROOT = Path(__file__).resolve().parents[2]
BASE = "1" * 40
TESTED = "2" * 40


@pytest.fixture(autouse=True)
def isolated_image_preflight(monkeypatch):
    """Keep controller subprocess mocks separate from the independently tested image checks."""
    monkeypatch.setattr(check_images, "check_images", lambda *arguments: None)
    monkeypatch.setattr(check_suite_completion, "write_completion", lambda *arguments: None)


def test_ha_framework_and_provisioning_share_the_exact_base_image():
    """Avoid pulling a second HA runtime and testing onboarding against a different release."""
    framework_recipe = (ROOT / "Dockerfile.chain-test").read_text(encoding="utf-8")
    provisioning_recipe = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    framework_image = re.findall(
        r"^FROM (ghcr\.io/home-assistant/home-assistant:\S+)$", framework_recipe, re.MULTILINE
    )
    provisioning_image = re.findall(
        r"^    image: (ghcr\.io/home-assistant/home-assistant:\S+)$",
        provisioning_recipe,
        re.MULTILINE,
    )
    assert len(framework_image) == 1
    assert provisioning_image == framework_image


def test_browser_image_client_and_developer_runtime_share_playwright_version():
    """Keep installed browser revisions compatible with the Python client in CI and development."""
    browser_recipe = (ROOT / "Dockerfile.ui-test").read_text(encoding="utf-8")
    developer_recipe = (ROOT / ".devcontainer/Dockerfile.supervisor").read_text(encoding="utf-8")
    image_version = re.findall(
        r"^FROM mcr\.microsoft\.com/playwright/python:v([\d.]+)-", browser_recipe, re.MULTILINE
    )
    client_version = re.findall(r"playwright==([\d.]+)", browser_recipe)
    developer_version = re.findall(r"playwright==([\d.]+)", developer_recipe)
    assert len(image_version) == 1
    assert client_version == image_version
    assert developer_version == image_version


def raw_change(path, old_mode="100644", new_mode="100644", status="M"):
    """Represent one independent Git raw record, including NUL-safe paths and file modes."""
    return f":{old_mode} {new_mode} {'3' * 40} {'4' * 40} {status}\0{path}\0".encode()


def executed_suite(command):
    """Identify execution independently of optional pytest and native-controller arguments."""
    if "run" not in command or "--rm" not in command:
        return None
    if command[-1] == "ha-contracts":
        return "ha-contracts"
    return command[command.index("--rm") + 1]


@pytest.fixture
def git_diff(monkeypatch):
    """Supply raw tree changes while checking that the complete tested merge tree is compared."""
    changes = []

    def read_diff(command, **kwargs):
        """Reject incremental or rename-detecting comparisons that could conceal runtime changes."""
        assert command == [
            "git",
            "diff",
            "--raw",
            "--no-abbrev",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "-z",
            BASE,
            TESTED,
            "--",
        ]
        assert kwargs == {"cwd": ".", "stderr": subprocess.DEVNULL, "timeout": 60}
        return b"".join(changes)

    monkeypatch.setattr("script.ci.proof_git.subprocess.check_output", read_diff)
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: {})
    monkeypatch.setattr("script.ci.ci_scope.immutable_blob", lambda *args: b"")
    return changes


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "CONTRIBUTING.md",
        "AGENTS.md",
        "docs/guide.md",
        "design/nested/spec.md",
        "docs/a b\nc.md",
        "docs/image.png",
        "docs/verification/mobile.webp",
        "docs/verification/diagram.svg",
        "docs/guide.MD",
        "docs/reference.pdf",
        "docs/example.json",
        "docs/data.csv",
        "docs/site/provisioning/index.html",
        "docs/site/provisioning/start.mjs",
        "docs/site/provisioning/style.css",
        "docs/extensionless",
    ],
)
@pytest.mark.parametrize(
    "modes", [("100644", "100644", "M"), ("000000", "100644", "A"), ("100644", "000000", "D")]
)
def test_regular_documentation_changes_use_focused_checks(git_diff, path, modes):
    """All regular docs files and established root Markdown retain documentation-only checks."""
    git_diff.append(raw_change(path, *modes))
    assert documentation_only(BASE, TESTED)


@pytest.mark.parametrize(
    "path", [".github/workflows/developer-page.yaml", ".github/workflows/pages.yaml"]
)
def test_pages_publishers_retain_frontend_and_browser_validation(git_diff, path):
    """The renamed site publisher retains real frontend/browser coverage without platform tests."""
    from script.ci.ci_scope import selected_suites

    git_diff.append(raw_change(path, "100644", "100644", "M"))
    assert selected_suites(select_profile(BASE, TESTED)) == {"ci", "unit-frontend", "browser"}


@pytest.mark.parametrize("module", ["model.py", "collection.py", "api.py", "serialization.py"])
@pytest.mark.parametrize(
    "modes", [("100644", "100644", "M"), ("000000", "100644", "A"), ("100644", "000000", "D")]
)
def test_notification_projection_owner_keeps_application_and_browser_contracts(
    git_diff, module, modes
):
    """Read-only notification infrastructure covers all app consumers and frontend contracts without platform lifecycle."""
    from script.ci.ci_scope import selected_suites

    git_diff.append(raw_change(f"src/open_energy_planner/notifications/{module}", *modes))
    assert selected_suites(select_profile(BASE, TESTED)) == {
        "ci",
        "unit-app",
        "unit-frontend",
        "browser",
    }


@pytest.mark.parametrize(
    "module", ["planning_notifications.py", "version_notifications.py", "future.py"]
)
@pytest.mark.parametrize(
    "modes", [("100644", "100644", "M"), ("000000", "100644", "A"), ("100644", "000000", "D")]
)
def test_diagnostic_sources_keep_browser_payload_consumers_without_full_ci(git_diff, module, modes):
    """Existing and new diagnostics retain app, process and browser proof for additions, edits and removals."""
    from script.ci.ci_scope import selected_suites

    git_diff.append(raw_change(f"src/open_energy_planner/runtime/diagnostics/{module}", *modes))
    assert selected_suites(select_profile(BASE, TESTED)) == {
        "ci",
        "unit-app",
        "unit-supervisor",
        "browser",
    }


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "script/ci/ci_scope.py",
        ".github/workflows/checks.yaml",
        "requirements.lock",
        "compose.yaml",
        "THIRD_PARTY_NOTICES.md",
        "unknown.md",
        "ha_app/README.md",
        "docs-assets/image.png",
        "design/ENGLISH-MIGRATION.json",
    ],
)
def test_any_other_change_requires_full_checks(git_diff, path):
    """A documentation edit cannot hide any runtime, infrastructure, asset or unclassified change."""
    git_diff.extend((raw_change(path), raw_change("README.md")))
    assert not documentation_only(BASE, TESTED)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/open_energy_planner/planning_policy.py", "full"),
        ("src/open_energy_planner/web/public/contract.js", "selected:browser,ci,unit-frontend"),
    ],
)
def test_documentation_assets_preserve_other_required_suites(git_diff, path, expected):
    """Screenshots add no runtime impact and cannot narrow another changed owner's requirements."""
    git_diff.extend((raw_change("docs/verification/mobile.png"), raw_change(path)))
    assert select_profile(BASE, TESTED) == expected


@pytest.mark.parametrize("special_mode", ["100755", "120000", "160000"])
@pytest.mark.parametrize(
    "modes",
    [("000000", None, "A"), (None, "000000", "D"), (None, "100644", "T"), ("100644", None, "T")],
)
def test_special_modes_never_use_focused_checks(git_diff, special_mode, modes):
    """Executable files, symlinks and gitlinks require full checks even when named like documentation."""
    old_mode, new_mode, status = modes
    git_diff.append(
        raw_change("docs/guide.md", old_mode or special_mode, new_mode or special_mode, status)
    )
    assert not documentation_only(BASE, TESTED)


@pytest.mark.parametrize(
    "old_path,expected", [("docs/old.md", True), ("src/app.py", False), ("README.md", True)]
)
def test_renames_check_both_original_and_destination(git_diff, old_path, expected):
    """Two-tree no-rename output admits document moves but never a source-to-document rename."""
    git_diff.extend(
        (
            raw_change(old_path, "100644", "000000", "D"),
            raw_change("docs/new.md", "000000", "100644", "A"),
        )
    )
    assert documentation_only(BASE, TESTED) is expected


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"broken",
        b"broken\0",
        b"broken\0README.md\0",
        b":100644 100644 blob blob M\0docs/\xff.md\0",
    ],
)
def test_empty_or_unreadable_diff_requires_full_checks(monkeypatch, output):
    """Malformed or empty Git output cannot grant eligibility for fewer checks."""
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: output
    )
    assert not documentation_only(BASE, TESTED)


@pytest.mark.parametrize("revision", [None, "", "--help", "HEAD", "a" * 39, "A" * 40])
def test_invalid_revisions_fail_closed(revision):
    """Only immutable full Git object identities may reach the comparison command."""
    assert not documentation_only(revision, TESTED)
    assert not documentation_only(BASE, revision)


def test_git_failure_requires_full_checks(monkeypatch):
    """Missing objects or unavailable Git select full checks instead of suppressing validation."""

    def missing_object(*args, **kwargs):
        """Simulate Git rejecting an unavailable immutable revision."""
        raise subprocess.CalledProcessError(128, "git diff")

    monkeypatch.setattr("script.ci.proof_git.subprocess.check_output", missing_object)
    assert not documentation_only(BASE, TESTED)


@pytest.mark.parametrize(
    "event_name,payload,expected",
    [
        ("pull_request", {"pull_request": {"base": {"sha": BASE}}}, True),
        ("push", {}, False),
        ("workflow_dispatch", {}, False),
        ("pull_request", {}, False),
        ("pull_request", [], False),
        ("pull_request", "invalid JSON", False),
    ],
)
def test_actions_output_defaults_to_full_except_valid_doc_pr(
    git_diff, monkeypatch, tmp_path, event_name, payload, expected
):
    """PR base and checked-out merge SHA determine scope. Other events and unreadable payloads stay full."""
    git_diff.append(raw_change("README.md"))
    event_path = tmp_path / "event.json"
    event_path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    output_path = tmp_path / "output"
    for name, value in {
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_SHA": TESTED,
        "GITHUB_OUTPUT": str(output_path),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    main()
    report = json.loads((tmp_path / "results/ci/selection.json").read_text(encoding="utf-8"))
    assert report["tested_commit"] == TESTED
    outputs = dict(line.split("=", 1) for line in output_path.read_text().splitlines())
    assert outputs["documentation_only"] == str(expected).lower()
    assert outputs["profile"] == ("documentation" if expected else "full")
    assert outputs["targets"] == ("native" if expected else "native,app,ha-tests,ui-tests")


@pytest.mark.parametrize("focused", [False, True])
def test_native_scope_keeps_lint_and_only_omits_unaffected_suites(monkeypatch, focused):
    """Focused checks retain Ruff and documentation/routing tests. Full scope retains smoke/scenarios."""
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(
        sys, "argv", ["check_native.py", *(["--documentation-only"] if focused else [])]
    )
    runpy.run_path(str(ROOT / "script/ci/check_native.py"), run_name="__main__")
    assert calls[0][1:4] == ["-m", "ruff", "check"]
    assert calls[1][1:5] == ["-m", "ruff", "format", "--check"]
    assert calls[0][4:] == [
        "src",
        "custom_components",
        "tests",
        "script",
        "tests/frontend",
        "test_environment",
        "docs/site",
    ]
    if focused:
        assert len(calls) == 3
        assert calls[2][1:] == [
            "-m",
            "pytest",
            "-q",
            "tests/documentation",
            "tests/ci/test_ci_scope.py",
            "--junitxml=results/ci/documentation.xml",
            "-p",
            "no:cacheprovider",
        ]
    else:
        assert len(calls) == 5
        assert "--ignore=tests/custom_component/integration" in calls[2]
        assert calls[3][1:] == ["script/app/smoke_app.py"]
        assert calls[4][1:] == ["script/app/debug_scenario.py"]


@pytest.mark.parametrize("fail", [False, True])
def test_quick_checks_stop_before_any_test_runtime_or_docker_build(monkeypatch, fail):
    """Quick mode runs only the existing full-scope Ruff checks, failing immediately on error."""
    calls = []

    def run(command, **kwargs):
        """Capture commands and optionally reject the first check to prove early failure."""
        calls.append(command[1:])
        assert kwargs["check"] is True
        if fail:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["check_native.py", "--quick"])
    if fail:
        with pytest.raises(subprocess.CalledProcessError):
            runpy.run_path(str(ROOT / "script/ci/check_native.py"), run_name="__main__")
        assert calls == [
            [
                "-m",
                "ruff",
                "check",
                "src",
                "custom_components",
                "tests",
                "script",
                "tests/frontend",
                "test_environment",
                "docs/site",
            ]
        ]
    else:
        runpy.run_path(str(ROOT / "script/ci/check_native.py"), run_name="__main__")
        assert calls == [
            [
                "-m",
                "ruff",
                "check",
                "src",
                "custom_components",
                "tests",
                "script",
                "tests/frontend",
                "test_environment",
                "docs/site",
            ],
            [
                "-m",
                "ruff",
                "format",
                "--check",
                "src",
                "custom_components",
                "tests",
                "script",
                "tests/frontend",
                "test_environment",
                "docs/site",
            ],
        ]


@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("fail_build", [False, True])
@pytest.mark.parametrize("prebuilt", [False, True])
@pytest.mark.parametrize("performance", [False, True])
def test_orchestrator_preserves_scope_failure_and_cleanup(
    monkeypatch, focused, fail_build, prebuilt, performance
):
    """Run only native for docs, retain all four database/framework suites otherwise, and always clean up after failures."""
    calls = []
    performance = performance and not focused
    database_access = Lock()

    def run_command(command, **kwargs):
        """Record checked operations and optionally fail the build to prove error propagation and cleanup."""
        database_command = command[6:9] == ["run", "--rm", "db-tests"]
        if database_command:
            assert database_access.acquire(blocking=False), "Database suites overlap"
        try:
            calls.append((command, kwargs["check"]))
            if database_command:
                Event().wait(0.05)
            if fail_build and ("build" in command or (prebuilt and "native" in command)):
                raise subprocess.CalledProcessError(1, command)
        finally:
            if database_command:
                database_access.release()

    monkeypatch.setattr(subprocess, "run", run_command)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "oep-check-12345-1")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ci.py",
            *(["--documentation-only"] if focused else []),
            *(["--profile", "full+performance-queries"] if performance else []),
            *(["--prebuilt"] if prebuilt else []),
        ],
    )
    if fail_build:
        with pytest.raises(subprocess.CalledProcessError):
            runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    else:
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    supervisor = ([sys.executable, "-m", "script.supervisor.check_supervisor"], True)
    if not focused and not fail_build:
        assert calls.pop() == supervisor
    else:
        assert supervisor not in calls
    if prebuilt:
        assert calls[0][0][:3] == ["docker", "image", "inspect"]
        assert calls[0][1] is True
        assert calls[0][0][3:] == [
            f"oep-check-12345-1-{target}"
            for target in (
                ["native"]
                if focused
                else ["native", "app", "ha-tests", "provision-tests", "ui-tests", "db-tests"]
            )
        ]
        calls.pop(0)
    compose = calls[0][0][:6]
    assert compose[:3] == ["docker", "compose", "-p"]
    assert compose[3].startswith("oep-check-")
    if prebuilt:
        assert compose[3] == "oep-check-12345-1"
    operations = [command[6:] for command, _ in calls]
    assert operations[-2:] == [
        ["logs", "--no-color"],
        ["--profile", "test", "down", "--volumes", "--remove-orphans"],
    ]
    assert calls[-2][1] is False and calls[-1][1] is False
    if not fail_build:
        if focused:
            expected = [
                ["--profile", "test", "build", "native"],
                [
                    "run",
                    "--rm",
                    "native",
                    "script/ci/check_native.py",
                    "--documentation-only",
                ],
            ]
        else:
            expected = [
                [
                    "--profile",
                    "test",
                    "build",
                    "native",
                    "app",
                    "ha-tests",
                    "provision-tests",
                    "ui-tests",
                    "db-tests",
                ],
                ["run", "--rm", "ui-tests"],
                [
                    "run",
                    "--no-deps",
                    "--rm",
                    "ha-tests",
                    "-m",
                    "script.ci.pytest_suites",
                    "ha-contracts",
                ],
                [
                    "run",
                    "--rm",
                    "native",
                    "script/ci/check_native.py",
                    "--without-database-contracts",
                ],
                ["up", "--detach", "--wait", "postgres-test", "mariadb-test", "mysql-test"],
                ["run", "--rm", "db-tests"],
                ["run", "--rm", "db-tests", "-m", "script.ci.pytest_suites", "performance-queries"],
                ["up", "--no-build", "--detach", "--wait", "app"],
                [
                    "run",
                    "--rm",
                    "ha-tests",
                    "-m",
                    "script.ci.check_ha",
                    "--ignore=tests/custom_component/integration/contracts",
                ],
                ["run", "--rm", "provision-tests"],
            ]
        if not focused:
            for early_suite in ("native", "ui-tests"):
                command = ["run", "--rm", early_suite] + (
                    ["script/ci/check_native.py", "--without-database-contracts"]
                    if early_suite == "native"
                    else []
                )
                assert operations.count(command) == 1
                assert operations.index(command) < next(
                    index
                    for index, operation in enumerate(operations)
                    if operation[-1] == "ha-contracts"
                )
                operations.remove(command)
                expected.remove(command)
            assert operations.count(["run", "--rm", "db-tests"]) == 1
            operations.remove(["run", "--rm", "db-tests"])
            expected.remove(["run", "--rm", "db-tests"])
            performance_command = [
                "run",
                "--rm",
                "db-tests",
                "-m",
                "script.ci.pytest_suites",
                "performance-queries",
            ]
            assert operations.count(performance_command) == int(performance)
            if performance:
                operations.remove(performance_command)
            expected.remove(performance_command)
        assert operations[:-2] == (expected[1:] if prebuilt else expected)


def test_failed_supervisor_acceptance_fails_full_ci_after_fast_service_cleanup(monkeypatch):
    """The required actual-Supervisor gate cannot be ignored, and it starts after fast suite resources are released."""
    calls = []

    def run_command(command, **kwargs):
        """Let fast checks pass and independently fail the required platform acceptance process."""
        calls.append(command)
        if command[-1] == "script.supervisor.check_supervisor":
            assert calls[-2][-5:] == ["--profile", "test", "down", "--volumes", "--remove-orphans"]
            assert kwargs["check"] is True
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", run_command)
    monkeypatch.setattr(sys, "argv", ["ci.py"])
    with pytest.raises(subprocess.CalledProcessError):
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")


@pytest.mark.parametrize("project", ["", "oep-user-test", "oep-check-../live", "oep-check-12345"])
def test_prebuilt_rejects_missing_or_non_actions_project(monkeypatch, project):
    """Invalid prebuilt project names fail before running Docker or cleaning up another environment."""
    calls = []
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    monkeypatch.setattr(sys, "argv", ["ci.py", "--prebuilt"])
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(SystemExit, match="2"):
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    assert not calls


def test_prebuilt_missing_image_fails_before_any_suite(monkeypatch):
    """A missing loaded image fails the gate and cleans up, without silently rebuilding or skipping tests."""
    calls = []

    def missing_image(command, **kwargs):
        """Fail Docker's image check while allowing scoped teardown."""
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "oep-check-12345-1")
    monkeypatch.setattr(sys, "argv", ["ci.py", "--prebuilt"])
    monkeypatch.setattr(subprocess, "run", missing_image)
    with pytest.raises(subprocess.CalledProcessError):
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    assert len(calls) == 3
    assert calls[-2][-2:] == ["logs", "--no-color"]
    assert calls[-1][-3:] == ["down", "--volumes", "--remove-orphans"]


@pytest.mark.parametrize("failed_suite", [None, "db-tests", "ha-tests"])
def test_independent_database_and_ha_suites_overlap_and_join_before_cleanup(
    monkeypatch, failed_suite
):
    """Database and HA barriers prove actual overlap. Either failure fails CI after both finish and before teardown."""
    database_started, ha_started, database_finished, ha_finished = (
        Event(),
        Event(),
        Event(),
        Event(),
    )
    cleaned = []

    def isolated_suites(command, **kwargs):
        """Replace Docker with two cooperating processes. Sequential execution would fail their barriers."""
        if "run" in command and executed_suite(command) == "db-tests":
            database_started.set()
            assert ha_started.wait(5)
            database_finished.set()
            if failed_suite == "db-tests":
                raise subprocess.CalledProcessError(1, command)
        elif "run" in command and executed_suite(command) == "ha-tests":
            assert database_started.wait(5)
            ha_started.set()
            assert database_finished.wait(5)
            ha_finished.set()
            if failed_suite == "ha-tests":
                raise subprocess.CalledProcessError(1, command)
        elif "down" in command:
            assert database_finished.is_set() and ha_finished.is_set()
            cleaned.append(command)

    monkeypatch.setattr(sys, "argv", ["ci.py"])
    monkeypatch.setattr(subprocess, "run", isolated_suites)
    if failed_suite:
        with pytest.raises(subprocess.CalledProcessError):
            runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    else:
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    assert len(cleaned) == 1


@pytest.mark.parametrize(
    "profile,expected",
    [
        ("native", {"native"}),
        ("frontend", {"native", "ui-tests"}),
        ("ha", {"native", "ha-tests", "provision-tests", "ui-tests"}),
        ("storage", {"native", "ui-tests", "db-tests", "ha-tests", "provision-tests"}),
    ],
)
def test_selected_orchestration_executes_required_suites_and_scopes_cleanup(
    monkeypatch, profile, expected
):
    """Execute each route with a fake process runner and assert actual suite calls rather than configuration literals."""
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", profile])
    runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    actual = {
        command[command.index("--rm") + 1]
        for command in calls
        if command[:2] == ["docker", "compose"] and "run" in command
    }
    assert actual == expected
    assert not any(command[-1] == "script.supervisor.check_supervisor" for command in calls)
    assert any("postgres-test" in command for command in calls) == (profile == "storage")
    assert calls[-1][-5:] == ["--profile", "test", "down", "--volumes", "--remove-orphans"]


def test_selected_ha_failure_propagates_and_cleans_up(monkeypatch):
    """An affected suite failure cannot produce a passing narrowed candidate or skip scoped teardown."""
    calls = []

    def fail_ha(command, **kwargs):
        """Reject the selected real HA process and record subsequent cleanup."""
        calls.append(command)
        if executed_suite(command) == "ha-tests":
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", fail_ha)
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", "ha"])
    with pytest.raises(subprocess.CalledProcessError):
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    assert calls[-1][-3:] == ["down", "--volumes", "--remove-orphans"]


@pytest.mark.parametrize("failed_gate", ["ui-tests", "ha-contracts", "native"])
def test_early_behavior_failure_blocks_every_heavy_suite(monkeypatch, failed_gate):
    """Stale browser mocks, HA contracts and calculations stop before DB, full HA or Supervisor."""
    calls = []

    def run(command, **options):
        """Fail one early execution boundary, while retaining cleanup and all attempted work."""
        calls.append(command)
        if "run" in command and (
            command[-1] == failed_gate or executed_suite(command) == failed_gate
        ):
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["ci.py"])
    with pytest.raises(subprocess.CalledProcessError):
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    assert not any("up" in command for command in calls)
    assert not any(
        (
            "run" in command
            and executed_suite(command) in {"db-tests", "ha-tests", "provision-tests"}
        )
        or command[-1] == "script.supervisor.check_supervisor"
        for command in calls
    )
    assert calls[-1][-5:] == ["--profile", "test", "down", "--volumes", "--remove-orphans"]


@pytest.mark.parametrize(
    "profile",
    [
        "ci",
        "native",
        "frontend",
        "ha",
        "storage",
        "integration",
        "full",
        "full+performance-queries",
    ],
)
def test_orchestration_records_every_required_success_before_completion(monkeypatch, profile):
    """The final invariant receives only successful commands, including HA sentinels and provisioning."""
    completed = []
    monkeypatch.setattr(subprocess, "run", lambda command, **options: None)
    monkeypatch.setattr(
        check_suite_completion,
        "write_completion",
        lambda selected, suites: completed.append((selected, suites.copy())),
    )
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", profile])
    runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    assert completed == [(profile, check_suite_completion.required_suites(profile))]


def test_selected_suite_without_execution_fails_final_invariant(monkeypatch):
    """A newly selected suite missing from the controller cannot silently publish a successful candidate."""
    import importlib

    from script.ci import ci_scope

    real_completion = importlib.reload(check_suite_completion).write_completion
    monkeypatch.setitem(ci_scope.PROFILES, "native", frozenset({"native", "unimplemented-suite"}))
    monkeypatch.setattr(check_suite_completion, "write_completion", real_completion)
    monkeypatch.setattr(subprocess, "run", lambda command, **options: None)
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", "native"])
    with pytest.raises(RuntimeError, match="mismatch"):
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")


@pytest.mark.parametrize("failed", [None, "native", "ui-tests"])
def test_native_and_browser_early_gates_overlap_and_join_before_heavy_work(monkeypatch, failed):
    """Independent early stores/reports overlap; either failure blocks HA/DB until both have stopped."""
    native_started, browser_started = Event(), Event()
    native_finished, browser_finished = Event(), Event()

    def run(command, **options):
        """Barriers fail if the runner serializes the suites or tears down active early work."""
        if "run" in command and executed_suite(command) == "native":
            native_started.set()
            assert browser_started.wait(5)
            native_finished.set()
            if failed == "native":
                raise subprocess.CalledProcessError(1, command)
        elif "run" in command and executed_suite(command) == "ui-tests":
            browser_started.set()
            assert native_started.wait(5)
            browser_finished.set()
            if failed == "ui-tests":
                raise subprocess.CalledProcessError(1, command)
        elif "up" in command or "down" in command or command[-1] == "ha-contracts":
            assert native_finished.is_set() and browser_finished.is_set()

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["ci.py"])
    if failed:
        with pytest.raises(subprocess.CalledProcessError):
            runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    else:
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")


@pytest.mark.parametrize(
    "path",
    ["src/open_energy_planner/new_storage.py", "src/open_energy_planner/new_domain/storage.py"],
)
@pytest.mark.parametrize(
    "old_mode,new_mode,status", [("000000", "100644", "A"), ("100644", "000000", "D")]
)
def test_storage_spelling_cannot_hide_unknown_or_shared_ownership(
    git_diff, path, old_mode, new_mode, status
):
    """Persistence-like filenames cannot grant a focused route to undeclared shared/domain owners."""
    git_diff.append(raw_change(path, old_mode, new_mode, status))
    assert matrix_profile(select_profile(BASE, TESTED)) == "full"


def test_summary_includes_focused_documentation_and_early_ha_suites(monkeypatch, tmp_path):
    """Every independently invoked suite appears, including docs-only and the early HA sentinel."""
    from script.ci.ci_scope import selection_summary
    from script.ci.scope_report import write_selection_summary

    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    write_selection_summary(selection_summary("documentation", [("documentation", "README.md")]))
    assert "| documentation | Selected | `README.md`" in summary.read_text()
    summary.unlink()
    write_selection_summary(
        selection_summary("ha", [("ha", "custom_components/open_energy_planner/coordinator.py")])
    )
    assert "| ha-contracts | Selected |" in summary.read_text()


def test_build_failure_removes_stale_completion_before_any_suite(monkeypatch, tmp_path):
    """Even the first build failing cannot leave an earlier completion report eligible."""
    report = tmp_path / "suite-completion.json"
    report.write_text("old proof")
    monkeypatch.setattr(check_suite_completion, "REPORT", report)
    calls = []

    def fail_build(command, **options):
        """Reject the first build while allowing the scoped cleanup to run."""
        calls.append(command)
        if options["check"]:
            assert command[6:9] == ["--profile", "test", "build"]
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", fail_build)
    monkeypatch.setattr(sys, "argv", ["ci.py"])
    with pytest.raises(subprocess.CalledProcessError):
        runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    assert len(calls) == 3
    assert calls[-2][-2:] == ["logs", "--no-color"]
    assert calls[-1][-3:] == ["down", "--volumes", "--remove-orphans"]
    assert not report.exists()
