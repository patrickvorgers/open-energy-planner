"""Preserve exact test selection and stop packaged-load failures before long suites."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from script.ci import check_images
from script.ci.pytest_suites import pytest_arguments


def test_ha_image_default_uses_the_same_directory_runner_as_compose():
    """A bare image cannot retain a deleted scenario path while Compose masks its stale default."""
    recipe = (Path(__file__).resolve().parents[2] / "Dockerfile.chain-test").read_text(
        encoding="utf-8"
    )
    command = next(
        line.removeprefix("CMD ") for line in recipe.splitlines() if line.startswith("CMD ")
    )
    assert json.loads(command) == ["-m", "script.ci.check_ha"]


@pytest.mark.parametrize(
    "suite", ["native", "documentation", "databases", "browser", "ha-contracts"]
)
def test_collection_changes_only_execution_mode_and_report(suite):
    """Collection retains execution's files, filters and plugins while replacing its result report."""
    real = pytest_arguments(suite, arguments=("-k", "retained"))
    collected = pytest_arguments(suite, arguments=("-k", "retained"), collect_only=True)
    assert "--collect-only" in collected
    assert [argument for argument in real if not argument.startswith("--junitxml=")] == [
        argument for argument in collected if argument != "--collect-only"
    ]
    assert collected[-2:] == ["-k", "retained"]


@pytest.mark.parametrize("worker", [0, 1])
def test_ha_collection_uses_the_real_worker_partition(worker):
    """Each preflight worker loads the same HA files and partition plugin as its real process."""
    arguments = pytest_arguments("ha", worker=worker, collect_only=True)
    assert arguments[arguments.index("--ha-shard") + 1] == str(worker)
    assert "tests/custom_component/integration" in arguments
    assert arguments[arguments.index("-p") + 1] == "script.ci.ha_shard"


@pytest.mark.parametrize("failure", [False, True])
def test_packaged_collection_preserves_diagnostics_and_stops_on_failure(
    monkeypatch, tmp_path, failure
):
    """Real command boundaries forbid dependencies and builds. A load error is retained and blocks later suites."""
    monkeypatch.chdir(tmp_path)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    calls = []

    def run(command, **options):
        """Emulate a packaged missing import without starting Docker or test fixtures."""
        calls.append(command)
        assert "--no-deps" in command
        assert command[command.index("--pull") + 1] == "never"
        assert "--build" not in command
        assert "--collect-only" in command
        assert options == {"capture_output": True, "text": True, "check": False}
        return subprocess.CompletedProcess(
            command,
            2 if failure else 0,
            "ModuleNotFoundError: missing_fixture_module\n" if failure else "4 tests collected\n",
            "",
        )

    monkeypatch.setattr(check_images.subprocess, "run", run)
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            check_images.check_images(["docker", "compose"], "oep-test", "frontend", {"browser"})
        assert len(calls) == 1
        assert "missing_fixture_module" in summary.read_text()
        assert "missing_fixture_module" in Path("results/ci/preflight-native.log").read_text()
    else:
        check_images.check_images(["docker", "compose"], "oep-test", "frontend", {"browser"})
        assert len(calls) == 2
        assert calls[0][calls[0].index("--entrypoint") + 2 :] == [
            "native",
            *pytest_arguments("native", collect_only=True),
        ]
        assert calls[1][calls[1].index("--entrypoint") + 2 :] == [
            "ui-tests",
            *pytest_arguments("browser", collect_only=True),
        ]
        assert "oep-test-ui-tests" in summary.read_text()
        assert summary.read_text().count("**PASS**") == 2


def test_failed_preflight_prevents_long_suites_but_keeps_cleanup(monkeypatch, tmp_path):
    """A failed image gate reaches logs and project cleanup without starting native, DB, HA or browser execution."""
    root = Path(__file__).resolve().parents[2]
    calls = []
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", "frontend"])
    monkeypatch.setattr(subprocess, "run", lambda command, **options: calls.append(command))

    def reject(*arguments):
        """Represent a discovered missing packaged module before any real tests start."""
        raise subprocess.CalledProcessError(2, ["collect"])

    monkeypatch.setattr(check_images, "check_images", reject)
    with pytest.raises(subprocess.CalledProcessError):
        runpy.run_path(str(root / "script/ci/ci.py"), run_name="__main__")
    assert not any("run" in command for command in calls)
    assert any("logs" in command for command in calls)
    assert calls[-1][-3:] == ["down", "--volumes", "--remove-orphans"]


def test_successful_full_preflight_stops_before_suites_and_supervisor(monkeypatch):
    """Successful full-profile collection must not start long tests or real Supervisor acceptance."""
    root = Path(__file__).resolve().parents[2]
    calls = []
    profiles = []
    monkeypatch.setattr(sys, "argv", ["ci.py", "--profile", "full", "--stage", "preflight"])
    monkeypatch.setattr(subprocess, "run", lambda command, **options: calls.append(command))
    monkeypatch.setattr(
        check_images,
        "check_images",
        lambda compose, project, profile, suites: profiles.append(profile),
    )
    runpy.run_path(str(root / "script/ci/ci.py"), run_name="__main__")
    assert profiles == ["full"]
    assert not any("run" in command for command in calls)
    assert not any("script.supervisor.check_supervisor" in command for command in calls)
    assert calls[-1][-3:] == ["down", "--volumes", "--remove-orphans"]
