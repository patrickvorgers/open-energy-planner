"""Personal Linux CI must preserve the host and clean only its own disposable project."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from script.ci import local_runner


def test_local_runner_uses_linux_containers_without_mutating_docker(monkeypatch, tmp_path):
    """The runner retains daemon configuration and exports only the verified local transport."""
    calls = []
    output = tmp_path / "github-env"
    monkeypatch.setattr(local_runner.platform, "system", lambda: "Linux")
    monkeypatch.setenv("GITHUB_ENV", str(output))
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setattr(local_runner, "require_local_engine", lambda: calls.append("verify-local"))

    def info(command, **options):
        """Expose only the independent Linux engine identity needed by preparation."""
        calls.append(command)
        return json.dumps({"OSType": "linux", "Architecture": "x86_64"})

    def run(command, **options):
        """Reject any mutation or container operation during runner preparation."""
        calls.append(command)
        assert command == ["docker", "compose", "version"]

    monkeypatch.setattr(local_runner.subprocess, "check_output", info)
    monkeypatch.setattr(local_runner.subprocess, "run", run)
    local_runner.prepare()
    assert calls[0] == "verify-local"
    assert len(calls) == 3
    assert "DOCKER_CONTEXT=\n" in output.read_text()
    assert "DOCKER_HOST=" in output.read_text()


@pytest.mark.parametrize(
    "engine",
    [
        {"OSType": "windows", "Architecture": "x86_64"},
        {"OSType": "linux", "Architecture": "aarch64"},
    ],
)
def test_wrong_container_platform_cannot_start_personal_ci(monkeypatch, engine):
    """Native Windows containers and ARM engines cannot claim AMD64 container proof."""
    monkeypatch.setattr(local_runner.platform, "system", lambda: "Linux")
    monkeypatch.setattr(local_runner, "require_local_engine", lambda: None)
    monkeypatch.setattr(
        local_runner.subprocess, "check_output", lambda *args, **kwargs: json.dumps(engine)
    )
    with pytest.raises(RuntimeError, match="Linux x64 Docker"):
        local_runner.prepare()


@pytest.mark.parametrize("host", ["Darwin", "Windows"])
def test_unsupported_host_is_rejected_before_docker_access(monkeypatch, host):
    """Native Windows and macOS cannot bypass the Linux orchestration contract."""
    monkeypatch.setattr(local_runner.platform, "system", lambda: host)

    def unexpected():
        """No Docker access is permitted on an unsupported host."""
        pytest.fail("Unsupported platform accessed Docker")

    monkeypatch.setattr(local_runner, "require_local_engine", unexpected)
    with pytest.raises(RuntimeError, match="Linux with Python"):
        local_runner.prepare()


def test_old_python_is_rejected_before_docker_access(monkeypatch):
    """An obsolete interpreter cannot run the standard-library orchestration."""
    monkeypatch.setattr(local_runner.platform, "system", lambda: "Linux")
    monkeypatch.setattr(local_runner.sys, "version_info", (3, 11))
    with pytest.raises(RuntimeError, match="Python 3.12"):
        local_runner.prepare()


@pytest.mark.parametrize(
    "project", ["", "oep-dev", "other-project", "oep-check-1-2;prune", "oep-check-../../"]
)
def test_cleanup_rejects_non_owned_projects_before_engine_access(monkeypatch, project):
    """A missing or malformed Actions identity can never remove development or production resources."""
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)

    def unexpected():
        """No engine access is permitted until ownership has been established."""
        pytest.fail("Unowned cleanup accessed Docker")

    monkeypatch.setattr(local_runner, "require_local_engine", unexpected)
    with pytest.raises(RuntimeError, match="exact Actions project"):
        local_runner.cleanup()


def test_cleanup_removes_only_current_project_and_its_tagged_images(monkeypatch):
    """Shared caches, unrelated images and persistent environment volumes are never pruned."""
    commands = []
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "oep-check-123-2")
    monkeypatch.setattr(local_runner, "require_local_engine", lambda: None)

    def run(command, **options):
        """Model two present run images while refusing any unscoped Docker mutation."""
        commands.append(command)
        if command[1:3] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=0
                if command[-1] in {"oep-check-123-2-native", "oep-check-123-2-app"}
                else 1
            )
        if command[1:3] == ["image", "rm"]:
            assert command[-1] in {"oep-check-123-2-native", "oep-check-123-2-app"}
        else:
            assert command == [
                "docker",
                "compose",
                "-p",
                "oep-check-123-2",
                "-f",
                "compose.yaml",
                "--profile",
                "test",
                "down",
                "--volumes",
                "--remove-orphans",
            ]
        assert options["check"] is True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(local_runner.subprocess, "run", run)
    local_runner.cleanup()
    assert len(commands) == 9


def test_workflow_keeps_runner_credential_on_hosted_admission():
    """Private runner lookup credentials and label writes never enter the personal test job."""
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/checks.yaml").read_text()
    candidate = workflow.split("  candidate:\n", 1)[1].split("  quality:\n", 1)[0]
    admission = (root / ".github/workflows/ci-admission.yaml").read_text()
    quality = workflow.split("  quality:\n", 1)[1].split("  execution-label:\n", 1)[0]
    assert "uses: ./.github/workflows/ci-admission.yaml" in candidate
    assert "OEP_RUNNER_READ_TOKEN: ${{ secrets.OEP_RUNNER_READ_TOKEN }}" in candidate
    assert "runs-on: ubuntu-24.04" in admission
    assert "OEP_RUNNER_READ_TOKEN: ${{ secrets.OEP_RUNNER_READ_TOKEN }}" in admission
    assert "value: ${{ jobs.candidate.outputs.runner_labels }}" in admission
    assert "runner_labels: ${{ steps.admission.outputs.runner_labels }}" in admission
    assert "OEP_RUNNER_READ_TOKEN" not in quality
    assert "pull-requests: write" not in quality
    assert "gh pr edit" not in quality
    assert "runs-on: ${{ fromJSON(needs.candidate.outputs.runner_labels) }}" in quality
    daemon = quality.split("- name: Enable shared build and container image storage\n", 1)[1].split(
        "- name:", 1
    )[0]
    assert "if: runner.environment == 'github-hosted'" in daemon
    assert "script.ci.local_runner cleanup" in quality
    assert "steps.scope.outputs.profile" in quality
    assert "script.ci.check_suite_completion" in quality


def test_personal_bootstrap_uses_container_python_and_hosted_setup_stays_hosted():
    """Local orchestration uses image dependencies without mutating the host Python installation."""
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/checks.yaml").read_text()
    for name, command in (
        ("Check personal Docker runner without changing its daemon", "prepare"),
        ("Clean only this personal runner test project", "cleanup"),
    ):
        step = workflow.split(f"- name: {name}\n", 1)[1].split("- name:", 1)[0]
        assert "runner.environment == 'self-hosted'" in step
        assert f"run: python -m script.ci.local_runner {command}" in step
        if command == "cleanup":
            assert "always()" in step
            assert "steps.execution.outputs.location == 'local'" in step
    setup = workflow.split("- name: Set up host orchestration Python\n", 1)[1].split("- name:", 1)[
        0
    ]
    assert "if: runner.environment == 'github-hosted'" in setup


@pytest.mark.skipif(sys.platform != "linux", reason="The runner entrypoint executes inside Linux")
@pytest.mark.parametrize(
    ("action", "repository", "work_path"),
    [
        ("unknown", "owner/repository", "/var/lib/docker/volumes/oep-ci-runner-work/_data"),
        (
            "register",
            "https://foreign.example/repo",
            "/var/lib/docker/volumes/oep-ci-runner-work/_data",
        ),
        ("run", "owner/repository", "/unrelated"),
        ("remove", "", "/var/lib/docker/volumes/oep-ci-runner-work/_data"),
    ],
)
def test_container_entrypoint_rejects_invalid_identity_before_docker(action, repository, work_path):
    """Bad operations, remote repository URLs and mismatched mounts fail without using credentials."""
    entrypoint = Path(__file__).resolve().parents[2] / ".github/runner/entrypoint.sh"
    result = subprocess.run(
        ["bash", str(entrypoint), action],
        check=False,
        env={
            **os.environ,
            "OEP_RUNNER_REPOSITORY": repository,
            "OEP_RUNNER_WORK_PATH": work_path,
        },
        input="private-registration-value\n",
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "private-registration-value" not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="The runner entrypoint executes inside Linux")
@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
@pytest.mark.parametrize("action", ["register", "remove"])
def test_container_token_accepts_eof_and_windows_lines_without_logging(action, ending):
    """Valid stdin reaches mount admission even without a final newline and never logs the token."""
    work_path = "/var/lib/docker/volumes/oep-ci-runner-work/_data"
    if os.path.isdir(work_path) and os.path.exists("/var/run/docker.sock"):
        pytest.skip("Run token admission in a namespace without the live runner mounts")
    result = subprocess.run(
        ["bash", str(Path(__file__).resolve().parents[2] / ".github/runner/entrypoint.sh"), action],
        check=False,
        env={
            **os.environ,
            "OEP_RUNNER_REPOSITORY": "owner/repository",
            "OEP_RUNNER_WORK_PATH": work_path,
        },
        input="PRIVATE_TEST_TOKEN" + ending,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "shared work volume and local Docker socket are required" in result.stderr
    assert "PRIVATE_TEST_TOKEN" not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="The runner entrypoint executes inside Linux")
@pytest.mark.parametrize("token", ["", "\n", "\r\n", "invalid token\n"])
def test_container_rejects_empty_or_malformed_token_before_mounts(token):
    """Missing or malformed credentials cannot reach filesystem initialization or registration."""
    result = subprocess.run(
        [
            "bash",
            str(Path(__file__).resolve().parents[2] / ".github/runner/entrypoint.sh"),
            "register",
        ],
        check=False,
        env={
            **os.environ,
            "OEP_RUNNER_REPOSITORY": "owner/repository",
            "OEP_RUNNER_WORK_PATH": "/var/lib/docker/volumes/oep-ci-runner-work/_data",
        },
        input=token,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert result.stderr.strip() == "A nonempty registration token is required."
