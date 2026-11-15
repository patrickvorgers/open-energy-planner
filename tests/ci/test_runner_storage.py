"""Exercise hosted runner headroom decisions without removing any real SDK files."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from script.ci import check_runner_storage as storage

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def hosted_runner(monkeypatch):
    """Model the supported hosted environment and record every destructive command."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("RUNNER_OS", "Linux")
    commands = []
    monkeypatch.setattr(
        storage.subprocess, "run", lambda *args, **kwargs: commands.append((args, kwargs))
    )
    return commands


@pytest.mark.parametrize("available", [50_000_000_000, 50_000_000_001, 86_000_000_000])
def test_sufficient_space_never_runs_cleanup(monkeypatch, hosted_runner, capsys, available):
    """The exact decimal 50 GB boundary and larger disks retain every SDK directory."""
    inspected = []

    def disk_usage(path):
        """Record the actual filesystem measured before deciding whether cleanup is needed."""
        inspected.append(path)
        return SimpleNamespace(free=available)

    monkeypatch.setattr(storage.shutil, "disk_usage", disk_usage)
    storage.main()
    assert inspected == ["/"]
    assert hosted_runner == []
    assert "Skipping unused SDK cleanup" in capsys.readouterr().out


@pytest.mark.parametrize("after", [50_000_000_000, 60_000_000_000])
def test_low_space_cleans_only_fixed_targets_then_remeasures(
    monkeypatch, hosted_runner, capsys, after
):
    """A disk one byte below the boundary must reclaim space and freshly verify the result."""
    measurements = iter([49_999_999_999, after])
    monkeypatch.setattr(
        storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=next(measurements))
    )
    storage.main()
    assert hosted_runner == [
        (
            (
                [
                    "sudo",
                    "rm",
                    "-rf",
                    "--",
                    "/usr/local/lib/android",
                    "/usr/share/dotnet",
                    "/opt/ghc",
                ],
            ),
            {"check": True},
        )
    ]
    assert "Runner storage after cleanup:" in capsys.readouterr().out


def test_cleanup_that_cannot_recover_enough_space_fails(monkeypatch, hosted_runner):
    """An insufficient fresh measurement stops CI instead of starting the expensive work."""
    measurements = iter([20_000_000_000, 49_999_999_999])
    monkeypatch.setattr(
        storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=next(measurements))
    )
    with pytest.raises(
        SystemExit, match="Insufficient runner storage after SDK cleanup"
    ) as failure:
        storage.main()
    assert len(hosted_runner) == 1
    assert "49999999999 bytes" in str(failure.value)
    assert "50000000000 bytes" in str(failure.value)


def test_failed_cleanup_is_not_reported_as_success(monkeypatch, hosted_runner, capsys):
    """A failing cleanup command propagates before any success or post-cleanup claim."""
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))

    def fail_cleanup(*args, **kwargs):
        """Represent a real failed privileged command without executing it."""
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(storage.subprocess, "run", fail_cleanup)
    with pytest.raises(subprocess.CalledProcessError):
        storage.main()
    assert "after cleanup" not in capsys.readouterr().out


def test_unreadable_disk_fails_without_cleanup(monkeypatch, hosted_runner):
    """An unavailable disk measurement cannot authorize deletion or expensive CI work."""

    def fail_measurement(path):
        """Represent a real filesystem measurement error."""
        raise OSError("Disk measurement unavailable")

    monkeypatch.setattr(storage.shutil, "disk_usage", fail_measurement)
    with pytest.raises(OSError, match="Disk measurement unavailable"):
        storage.main()
    assert hosted_runner == []


@pytest.mark.parametrize(
    "variable,value",
    [("GITHUB_ACTIONS", "false"), ("RUNNER_ENVIRONMENT", "self-hosted"), ("RUNNER_OS", "Windows")],
)
def test_other_environments_are_rejected_before_disk_access(
    monkeypatch, hosted_runner, variable, value
):
    """Local, self-hosted and Windows environments cannot reach the cleanup command."""
    monkeypatch.setenv(variable, value)

    def unexpected_disk_access(path):
        """Fail if an unsupported runner reaches even the first disk measurement."""
        pytest.fail("Unsupported environment inspected the disk")

    monkeypatch.setattr(storage.shutil, "disk_usage", unexpected_disk_access)
    with pytest.raises(SystemExit, match="GitHub-hosted Linux Actions job"):
        storage.main()
    assert hosted_runner == []


def test_workflow_uses_shared_guard_before_build_and_supervisor_work():
    """Both privileged cleanup sites retain their scope and run the same tested headroom guard."""
    workflow = (ROOT / ".github/workflows/checks.yaml").read_text(encoding="utf-8")
    preparation = workflow.split("      - name: Release unused hosted-runner SDK storage\n", 1)[1]
    preparation = preparation.split("      - name:", 1)[0]
    supervisor = workflow.split(
        "      - name: Release storage for actual Supervisor acceptance\n", 1
    )[1]
    supervisor = supervisor.split("      - name:", 1)[0]
    assert "if: steps.scope.outputs.documentation_only != 'true'" in preparation
    assert "if: matrix.lane == 'supervisor'" in supervisor
    for step in (preparation, supervisor):
        assert "run: python -m script.ci.check_runner_storage" in step
        assert "rm -rf" not in step
