"""Development orchestration must preserve data and reject the host Docker engine."""

import asyncio
import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from script.supervisor import dev, provision_supervisor


def test_development_provisioning_rejects_an_ordinary_host_before_docker(monkeypatch):
    """No development marker means no Docker command or filesystem mutation is allowed."""
    monkeypatch.delenv("OEP_DEVELOPER_ENVIRONMENT", raising=False)

    def unexpected_command(*arguments):
        """Fail if an unsafe caller reaches Docker at all."""
        pytest.fail("Docker must not run on an unmarked host")

    monkeypatch.setattr(provision_supervisor, "run", unexpected_command)
    with pytest.raises(RuntimeError, match="isolated oep-dev container"):
        provision_supervisor.require_private_engine()


@pytest.mark.parametrize("host_engine", ["outer-engine", "private-engine"])
def test_development_engine_identity_requires_private_docker(monkeypatch, host_engine):
    """An engine matching the host is rejected even when the container has the expected markers."""
    monkeypatch.setenv("OEP_DEVELOPER_ENVIRONMENT", "1")
    monkeypatch.setenv("DEVCONTAINER", "1")
    monkeypatch.setenv("OEP_HOST_DOCKER_ID", host_engine)
    monkeypatch.setattr(Path, "exists", lambda _path: True)
    monkeypatch.setattr(Path, "is_mount", lambda _path: True)
    monkeypatch.setattr(provision_supervisor.socket, "gethostname", lambda: "inner-container")
    monkeypatch.setattr(
        provision_supervisor,
        "run",
        lambda *arguments: json.dumps({"Name": "inner-container", "ID": "private-engine"}),
    )
    if host_engine == "private-engine":
        with pytest.raises(RuntimeError, match="outside the development container"):
            provision_supervisor.require_private_engine()
    else:
        assert provision_supervisor.require_private_engine() == "private-engine"


def test_source_identity_detects_renames_edits_and_removals_but_not_cache(tmp_path):
    """Warm starts reuse identical sources. Renaming, editing or removing code requires rebuilding."""
    module = tmp_path / "source.py"
    module.write_text("original\n")
    before = provision_supervisor.source_digest(tmp_path)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__/source.pyc").write_bytes(b"temporary")
    assert provision_supervisor.source_digest(tmp_path) == before
    module.write_text("changed\n")
    changed = provision_supervisor.source_digest(tmp_path)
    assert changed != before
    module.rename(tmp_path / "renamed.py")
    renamed = provision_supervisor.source_digest(tmp_path)
    assert renamed not in (before, changed)
    (tmp_path / "renamed.py").unlink()
    assert provision_supervisor.source_digest(tmp_path) not in (before, changed, renamed)


def test_source_replacement_removes_old_code_and_makes_private_staging_readable(
    tmp_path, monkeypatch
):
    """A replaced source directory loses stale code. Its public files are readable by the product's unprivileged user."""
    root = tmp_path / "supervisor"
    monkeypatch.setattr(provision_supervisor, "SUPERVISOR_ROOT", root)
    source = tmp_path / "source"
    source.mkdir()
    module = source / "new.py"
    module.write_text("new code\n")
    module.chmod(0o600)
    destination = root / "apps/local/open-energy-planner"
    destination.mkdir(parents=True)
    (destination / "obsolete.py").write_text("old code\n")
    provision_supervisor.public_tree(source, destination)
    assert (destination / "new.py").read_text() == "new code\n"
    assert not (destination / "obsolete.py").exists()
    assert (destination / "new.py").stat().st_mode & 0o444 == 0o444


def test_source_replacement_cannot_target_application_data(tmp_path, monkeypatch):
    """The build-context replacement helper must reject application data before deleting any file."""
    monkeypatch.setattr(provision_supervisor, "SUPERVISOR_ROOT", tmp_path)
    private = tmp_path / "apps/data/local_open_energy_planner"
    private.mkdir(parents=True)
    (private / "history.sqlite3").write_bytes(b"retain history")
    with pytest.raises(ValueError, match="outside the development source locations"):
        provision_supervisor.public_tree(tmp_path / "missing-source", private)
    assert (private / "history.sqlite3").read_bytes() == b"retain history"


def test_source_preparation_preserves_existing_home_assistant_configuration(tmp_path, monkeypatch):
    """Provisioning replaces product code without overwriting an existing HA configuration or automation."""
    repository = tmp_path / "repo"
    fixture = repository / "test_environment/oep_test_fixture"
    integration = repository / "custom_components/open_energy_planner"
    for directory in (fixture, integration):
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text('"""Test source."""\n')
    for name in ("configuration.yaml", "dashboard.yaml"):
        (repository / "test_environment" / name).write_text("sample: true\n")
    root = tmp_path / "supervisor"
    core = root / "homeassistant"
    core.mkdir(parents=True)
    saved = {
        "configuration.yaml": "homeassistant:\n  name: Keep my name\n",
        "automations.yaml": "- alias: Keep my automation\n",
        "oep-test.yaml": "title: Keep my dashboard\n",
    }
    for name, value in saved.items():
        (core / name).write_text(value)

    app = repository / ".local/dev/test-app"
    app.mkdir(parents=True)
    (app / "Dockerfile").write_text("FROM scratch\n")

    monkeypatch.setattr(provision_supervisor, "ROOT", repository)
    monkeypatch.setattr(provision_supervisor, "SUPERVISOR_ROOT", root)
    monkeypatch.setenv("OEP_SUPERVISOR_SMOKE", "1")
    before = provision_supervisor.prepare_sources({})
    after = provision_supervisor.prepare_sources(before)
    assert before == after
    for name, value in saved.items():
        assert (core / name).read_text() == value
    assert (core / "custom_components/open_energy_planner/__init__.py").is_file()
    assert (core / "custom_components/oep_test_fixture/__init__.py").is_file()


@pytest.mark.parametrize("action", ["stop", "status"])
def test_developer_commands_retain_volumes_and_target_only_the_developer_project(
    monkeypatch, action
):
    """Stop and status issue one scoped Compose command without deleting storage or starting services."""
    monkeypatch.delenv("OEP_DEVELOPER_ENVIRONMENT", raising=False)
    commands = []

    def run(command, **kwargs):
        """Record the actual process boundary without reaching Docker."""
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(dev.sys, "argv", ["dev.py", action])
    monkeypatch.setattr(dev, "require_local_engine", lambda: "local-engine")
    monkeypatch.setattr(dev.subprocess, "run", run)
    dev.main()
    assert commands == [
        [
            "docker",
            "compose",
            "-p",
            "oep-dev",
            "-f",
            "compose.dev.yaml",
            "stop" if action == "stop" else "ps",
        ]
    ]


@pytest.mark.parametrize(
    "stage", ["first", "warm", "update", "removed", "integration", "fixture", "failed_update"]
)
@pytest.mark.parametrize("late_mismatch", [False, True])
def test_supervisor_lifecycle_does_not_restart_initial_or_unchanged_core(
    tmp_path, monkeypatch, capsys, stage, late_mismatch
):
    """Reuse unchanged apps. Required refresh failures and late platform changes preserve the previous source marker."""
    monkeypatch.setattr(provision_supervisor.os, "environ", dict(provision_supervisor.os.environ))
    monkeypatch.setenv("OEP_SUPERVISOR_SMOKE", "0")
    monkeypatch.setattr(provision_supervisor, "SUPERVISOR_VERSION", "2026.10.5")
    monkeypatch.setattr(provision_supervisor, "CORE_VERSION", "2026.10.4")
    root = tmp_path / "developer"
    root.mkdir()
    original = {"app": "original", "integration": "original", "fixture": "original"}
    expected = dict(original)
    if stage in {"update", "failed_update"}:
        expected["app"] = "updated"
    elif stage in {"integration", "fixture"}:
        expected[stage] = "updated"
    if stage != "first":
        (root / "source.json").write_text(json.dumps(original))
    calls = []

    def ha(*arguments):
        """Expose only independent Supervisor lifecycle responses and record mutating commands."""
        calls.append(arguments)
        if arguments == ("supervisor", "info"):
            return {"version": "2026.10.5"}
        if arguments == ("core", "info"):
            return {"version": "2026.10.4"}
        if arguments == ("info",):
            return {"state": "running"}
        if arguments == ("apps",):
            return {
                "addons": []
                if stage in {"first", "removed"}
                else [{"slug": "local_open_energy_planner"}]
            }
        if arguments == ("store", "reload") and stage in {"warm", "integration", "fixture"}:
            pytest.fail("An unchanged installed app must not reload external app stores")
        if arguments == ("store", "reload") and stage == "failed_update":
            raise RuntimeError("fixture required app-store refresh failed")
        if arguments[:2] == ("apps", "info"):
            return {"state": "started"}
        return {}

    completed, image_checks = [], []
    package_checks = []

    async def complete():
        """Record actual onboarding/setup boundaries without using the real network."""
        completed.append("complete")

    def require_images():
        """Reject a digest change that occurs only after onboarding and sample setup finish."""
        image_checks.append(len(completed))
        if late_mismatch and len(completed) == 2:
            raise provision_supervisor.IsolationError("Platform changed during onboarding")

    def verify_package():
        """Record verification after setup and before the source success marker can change."""
        assert len(completed) == 2
        marker = root / "source.json"
        if stage == "first":
            assert not marker.exists()
        else:
            assert json.loads(marker.read_text()) == original
        package_checks.append("verified")
        return {"status": "matched"}

    monkeypatch.setattr(provision_supervisor, "DEVELOPER_ROOT", root)
    monkeypatch.setattr(provision_supervisor, "require_private_engine", lambda: True)
    monkeypatch.setattr(provision_supervisor, "prepare_sources", lambda previous: expected)
    monkeypatch.setattr(provision_supervisor, "prepare_core_image", lambda: None)
    monkeypatch.setattr(provision_supervisor, "require_platform_images", require_images)
    monkeypatch.setattr(provision_supervisor, "start_supervisor", lambda: None)
    monkeypatch.setattr(provision_supervisor, "ha", ha)
    monkeypatch.setattr(provision_supervisor.provision_ha, "provision", complete)
    monkeypatch.setattr(provision_supervisor, "finish_setup", complete)
    monkeypatch.setattr(provision_supervisor, "verify_installed_package", verify_package)
    if stage == "failed_update":
        with pytest.raises(RuntimeError, match="required app-store refresh failed"):
            provision_supervisor.main()
    elif late_mismatch:
        with pytest.raises(provision_supervisor.IsolationError, match="during onboarding"):
            provision_supervisor.main()
    else:
        provision_supervisor.main()
    assert image_checks == ([0] if stage == "failed_update" else [0, 2])
    assert package_checks == ([] if stage == "failed_update" else ["verified"])
    assert ("Real Supervisor developer environment ready." in capsys.readouterr().out) is not (
        late_mismatch or stage == "failed_update"
    )
    assert calls.count(("core", "restart")) == (
        1 if stage in {"update", "integration", "fixture", "failed_update"} else 0
    )
    assert calls.count(("store", "reload")) == (
        0 if stage in {"warm", "integration", "fixture"} else 1
    )
    assert calls.count(("apps", "rebuild", "local_open_energy_planner", "--force")) == (
        1 if stage == "update" else 0
    )
    assert calls.count(("apps", "install", "local_open_energy_planner")) == (
        1 if stage in {"first", "removed"} else 0
    )
    marker = root / "source.json"
    if late_mismatch and stage == "first":
        assert not marker.exists()
    else:
        assert json.loads(marker.read_text()) == (
            original if late_mismatch or stage == "failed_update" else expected
        )


def test_host_launcher_cannot_create_another_nested_environment(monkeypatch):
    """Calling the host launcher from the Dev Container must fail before touching either Docker engine."""
    monkeypatch.setenv("OEP_DEVELOPER_ENVIRONMENT", "1")

    def unexpected_command(*arguments, **kwargs):
        """Reject any accidental nested Compose invocation."""
        pytest.fail("The host launcher must not use the inner Docker engine")

    monkeypatch.setattr(dev.subprocess, "run", unexpected_command)
    with pytest.raises(RuntimeError, match="host terminal"):
        dev.main()


def test_recreated_developer_namespace_replaces_only_supervisor_owned_containers(
    tmp_path, monkeypatch
):
    """Stale host-network containers are recreated after an outer restart. An unrelated inner worker survives."""
    monkeypatch.setattr(provision_supervisor, "DEVELOPER_ROOT", tmp_path)
    monkeypatch.setattr(provision_supervisor, "SUPERVISOR_ROOT", tmp_path)
    (tmp_path / "engine-namespace").write_text("old-network-namespace")
    commands = []

    def run(*arguments):
        """Supply explicit container ownership at the actual Docker command boundary."""
        commands.append(arguments)
        if "ps" in arguments:
            if "--filter" in arguments:
                return "homeassistant\napp_local_open_energy_planner\nhassio_dns"
            return "homeassistant\napp_local_open_energy_planner\nhassio_dns\nhassio_supervisor\nmy-worker"
        return "started"

    monkeypatch.setattr(provision_supervisor, "run", run)
    provision_supervisor.start_supervisor()
    removed = [command for command in commands if "rm" in command]
    assert len(removed) == 1
    assert removed[0][-4:] == (
        "homeassistant",
        "app_local_open_energy_planner",
        "hassio_dns",
        "hassio_supervisor",
    )
    assert all("my-worker" not in command for command in commands)
    assert all("--volumes" not in command and "--force" not in command for command in commands)
    assert (tmp_path / "engine-namespace").read_text() != "old-network-namespace"


def test_sample_configuration_precedes_operational_connection_readiness(monkeypatch, capsys):
    """Initial sample settings clear an operational not-ready issue before the strict Supervisor check runs."""
    monkeypatch.setattr(provision_supervisor.os, "environ", dict(provision_supervisor.os.environ))
    events = []

    async def sample(*, app_url, token):
        """Verify private administration authority stays internal and initializes only the isolated app."""
        assert app_url == "http://172.30.33.0:8099"
        assert token == "private-test-token"
        events.append("sample")

    async def examples(*, app_url, token, journal_path):
        """Initialize device scenarios after the foundation without exposing private authority."""
        assert app_url == "http://172.30.33.0:8099" and token == "private-test-token"
        assert journal_path.name == "example-provisioning.json"
        events.append("examples")

    async def current_product(method, path):
        """Supply a fresh read used only for pending simulator-automation checks."""
        assert method == "GET" and path == "/v1/ev"
        return {"current": True}

    @asynccontextmanager
    async def product_requests(*, app_url, token):
        """Keep the private product transport open until HA control validation finishes."""
        assert app_url == "http://172.30.33.0:8099" and token == "private-test-token"
        events.append("app session")
        try:
            yield current_product
        finally:
            events.append("app closed")

    async def confirmed(*, examples_journal, example_request):
        """Require configuration to exist before evaluating the final healthy ownership projection."""
        assert events == ["sample", "examples", "app session"]
        assert examples_journal.name == "example-provisioning.json"
        assert await example_request("GET", "/v1/ev") == {"current": True}
        events.append("confirmed")

    monkeypatch.setattr(provision_supervisor, "ha", lambda *args: {"ip_address": "172.30.33.0"})
    monkeypatch.setattr(provision_supervisor, "run", lambda *args: "private-test-token")
    monkeypatch.setattr(
        provision_supervisor, "read_app", lambda *args: {"annual_estimate": {"days": 32}}
    )
    monkeypatch.setattr(provision_supervisor.provision_sample_settings, "provision", sample)
    monkeypatch.setattr(provision_supervisor.provision_device_examples, "provision", examples)
    monkeypatch.setattr(
        provision_supervisor.provision_device_examples, "product_requests", product_requests
    )
    monkeypatch.setattr(provision_supervisor.provision_ha, "provision", confirmed)
    asyncio.run(provision_supervisor.finish_setup())
    assert events == ["sample", "examples", "app session", "confirmed", "app closed"]
    assert "private-test-token" not in capsys.readouterr().out


def test_permanent_isolation_failure_does_not_enter_readiness_retry(monkeypatch):
    """An unmarked host is rejected immediately rather than waiting fifteen minutes or hiding its cause."""
    monkeypatch.delenv("OEP_DEVELOPER_ENVIRONMENT", raising=False)

    def unexpected_sleep(_delay):
        """Permanent ownership failures must never reach a readiness retry."""
        pytest.fail("An isolation failure must not be retried")

    monkeypatch.setattr(provision_supervisor, "sleep", unexpected_sleep)
    with pytest.raises(provision_supervisor.IsolationError, match="isolated oep-dev container"):
        provision_supervisor.wait_for(provision_supervisor.require_private_engine, "private engine")


def test_readiness_timeout_preserves_the_last_transient_cause(monkeypatch):
    """A bounded readiness failure retains the unavailable API cause instead of reporting only a generic timeout."""
    readings = iter((0, 0, 2))
    error = OSError("Local Supervisor API is not accepting connections")

    def unavailable():
        """Represent a transient socket failure while the isolated platform starts."""
        raise error

    monkeypatch.setattr(provision_supervisor, "monotonic", lambda: next(readings))
    monkeypatch.setattr(provision_supervisor, "sleep", lambda _delay: None)
    with pytest.raises(RuntimeError, match="Timed out waiting for Supervisor") as caught:
        provision_supervisor.wait_for(unavailable, "Supervisor", timeout=1)
    assert caught.value.__cause__ is error


def test_host_launcher_bounds_docker_identity_query_and_does_not_provision_after_timeout(
    monkeypatch,
):
    """A stalled short Docker query aborts the launcher before it can provision or copy credentials."""
    monkeypatch.delenv("OEP_DEVELOPER_ENVIRONMENT", raising=False)
    monkeypatch.setattr(dev.sys, "argv", ["dev.py", "start"])
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)

    def unexpected_prepare():
        """A stalled engine query must fail before replacing local staged sources."""
        pytest.fail("Sources were staged before confirming the Docker engine")

    monkeypatch.setattr(dev, "prepare", unexpected_prepare)
    commands = []
    monkeypatch.setattr(dev.subprocess, "run", lambda command, **kwargs: commands.append(command))

    def stalled(command, **kwargs):
        """Independently require the short call's one-minute bound before simulating a stalled daemon."""
        assert kwargs["timeout"] == 60
        if "context" in command:
            return "unix:///run/docker.sock"
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(dev.subprocess, "check_output", stalled)
    with pytest.raises(subprocess.TimeoutExpired):
        dev.main()
    assert commands == []


@pytest.mark.parametrize(
    "endpoint",
    ["tcp://remote.example:2376", "ssh://developer@remote.example", "tcp://localhost:2375"],
)
def test_remote_docker_host_is_rejected_before_source_staging_or_compose(monkeypatch, endpoint):
    """A selected TCP/SSH engine must not receive a privileged container or modify host staging."""
    monkeypatch.delenv("OEP_DEVELOPER_ENVIRONMENT", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", endpoint)
    monkeypatch.setattr(dev.sys, "argv", ["dev.py", "start"])

    def unexpected(*arguments, **kwargs):
        """Reject any attempt to reach Docker or replace staged sources for a remote transport."""
        pytest.fail("A remote engine passed the host-side boundary")

    monkeypatch.setattr(dev, "prepare", unexpected)
    monkeypatch.setattr(dev.subprocess, "run", unexpected)
    monkeypatch.setattr(dev.subprocess, "check_output", unexpected)
    with pytest.raises(RuntimeError, match="local Docker socket"):
        dev.main()


@pytest.mark.parametrize(
    "endpoint", ["unix:///var/run/docker.sock", "npipe:////./pipe/dockerDesktopLinuxEngine"]
)
def test_local_docker_context_is_checked_with_native_environment_precedence(monkeypatch, endpoint):
    """DOCKER_CONTEXT overrides DOCKER_HOST, and a local selected endpoint is queried with a bounded call."""
    monkeypatch.setenv("DOCKER_CONTEXT", "local-desktop")
    monkeypatch.setenv("DOCKER_HOST", "ssh://ignored.example")
    commands = []

    def query(command, **kwargs):
        """Expose an independently specified context endpoint and engine identity at the native CLI boundary."""
        assert kwargs["timeout"] == 60
        commands.append(command)
        if "context" not in command:
            assert dev.os.environ["DOCKER_HOST"] == endpoint
            assert "DOCKER_CONTEXT" not in dev.os.environ
        return endpoint if "context" in command else "local-engine-id"

    monkeypatch.setattr(dev.subprocess, "check_output", query)
    assert dev.require_local_engine() == "local-engine-id"
    assert len(commands) == 2


def test_remote_context_cannot_be_hidden_by_a_local_docker_host(monkeypatch):
    """An explicitly selected remote context takes precedence over a local DOCKER_HOST and is rejected."""
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-server")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")

    def query(command, **kwargs):
        """Only the selected context may be inspected before rejecting its remote transport."""
        assert "context" in command
        return "ssh://remote.example"

    monkeypatch.setattr(dev.subprocess, "check_output", query)
    with pytest.raises(RuntimeError, match="local Docker socket"):
        dev.require_local_engine()


@pytest.mark.parametrize(
    "existing", [None, {"auto_update": True, "channel": "stable", "supervisor": "2026.10.5"}]
)
def test_first_supervisor_boot_disables_updates_before_container_creation(
    tmp_path, monkeypatch, existing
):
    """An upstream release cannot race bootstrap. Retained updater settings survive the disabled auto-update option."""
    monkeypatch.setattr(provision_supervisor, "SUPERVISOR_ROOT", tmp_path)
    monkeypatch.setattr(provision_supervisor, "DEVELOPER_ROOT", tmp_path)
    updater = tmp_path / "updater.json"
    if existing is not None:
        updater.write_text(json.dumps(existing))
    commands = []

    def run(*arguments):
        """Observe real file contents at the Docker creation boundary, before an API can exist."""
        commands.append(arguments)
        if "ps" in arguments:
            return ""
        assert json.loads(updater.read_text()) == {**(existing or {}), "auto_update": False}
        assert arguments[-1] == provision_supervisor.SUPERVISOR_IMAGE
        return "created"

    monkeypatch.setattr(provision_supervisor, "run", run)
    provision_supervisor.start_supervisor()
    assert len(commands) == 2


def test_corrupt_updater_configuration_aborts_before_supervisor_start(tmp_path, monkeypatch):
    """Malformed retained configuration cannot silently enable default updates or be overwritten."""
    monkeypatch.setattr(provision_supervisor, "SUPERVISOR_ROOT", tmp_path)
    monkeypatch.setattr(provision_supervisor, "DEVELOPER_ROOT", tmp_path)
    updater = tmp_path / "updater.json"
    updater.write_text("broken")
    commands = []
    monkeypatch.setattr(
        provision_supervisor, "run", lambda *arguments: commands.append(arguments) or ""
    )
    with pytest.raises(json.JSONDecodeError):
        provision_supervisor.start_supervisor()
    assert updater.read_text() == "broken"
    assert len(commands) == 1


@pytest.mark.parametrize("running", [False, True])
def test_retained_supervisor_disables_updates_before_start_without_editing_live_config(
    tmp_path, monkeypatch, running
):
    """A stopped container receives the policy before restart. A running container keeps its live configuration untouched."""
    monkeypatch.setattr(provision_supervisor, "SUPERVISOR_ROOT", tmp_path)
    monkeypatch.setattr(provision_supervisor, "DEVELOPER_ROOT", tmp_path)
    monkeypatch.setattr(
        provision_supervisor, "run", lambda *arguments: "" if "ps" in arguments else "created"
    )
    provision_supervisor.start_supervisor()
    updater = tmp_path / "updater.json"
    updater.write_text(json.dumps({"auto_update": True, "channel": "stable"}))
    calls = []

    def run(*arguments):
        """Assert persisted policy at the restart command and distinguish stopped from running inventory."""
        calls.append(arguments)
        if "ps" in arguments:
            return "hassio_supervisor" if "--all" in arguments or running else ""
        assert arguments[-2:] == ("start", "hassio_supervisor")
        assert json.loads(updater.read_text()) == {"auto_update": False, "channel": "stable"}
        return "started"

    monkeypatch.setattr(provision_supervisor, "run", run)
    provision_supervisor.start_supervisor()
    assert len(calls) == (2 if running else 3)
    assert json.loads(updater.read_text())["auto_update"] is running


@pytest.mark.parametrize("retained", [False, True])
def test_core_image_uses_digest_only_for_fresh_configuration(tmp_path, monkeypatch, retained):
    """A new install uses the exact image bytes. Retained Core settings cannot trigger a platform upgrade."""
    monkeypatch.setattr(provision_supervisor, "SUPERVISOR_ROOT", tmp_path)
    monkeypatch.setattr(
        provision_supervisor,
        "CORE_IMAGE",
        "ghcr.io/home-assistant/home-assistant:2026.10.4@sha256:23f4dfbb69ce76315dbc4de7915504f1c2dcecd7ca2648113a493a385972e133",
    )
    monkeypatch.setattr(provision_supervisor, "CORE_VERSION", "2026.10.4")
    configuration = tmp_path / "homeassistant.json"
    original = {"image": "retained-image", "version": "2026.9.3", "port": 8123}
    if retained:
        configuration.write_text(json.dumps(original))
    calls = []
    monkeypatch.setattr(provision_supervisor, "run", lambda *arguments: calls.append(arguments))
    provision_supervisor.prepare_core_image()
    if retained:
        assert not calls
        assert json.loads(configuration.read_text()) == original
    else:
        assert calls[0][-2:] == (
            "pull",
            "ghcr.io/home-assistant/home-assistant:2026.10.4@sha256:23f4dfbb69ce76315dbc4de7915504f1c2dcecd7ca2648113a493a385972e133",
        )
        assert json.loads(configuration.read_text()) == {
            "image": "ghcr.io/home-assistant/home-assistant",
            "version": "2026.10.4",
            "override_image": True,
        }


@pytest.mark.parametrize("mismatch", [None, "homeassistant", "hassio_supervisor"])
def test_running_platform_images_require_exact_loaded_digest_identity(monkeypatch, mismatch):
    """Matching release labels cannot hide different Core or Supervisor image bytes."""
    expected = {
        "homeassistant": "sha256:core-bytes",
        "hassio_supervisor": "sha256:supervisor-bytes",
    }
    calls = []

    def run(*arguments):
        """Expose independent Docker image identities at the real inspect command boundary."""
        calls.append(arguments)
        if "image" in arguments:
            return (
                "sha256:core-bytes"
                if "home-assistant/home-assistant:" in arguments[-1]
                else "sha256:supervisor-bytes"
            )
        container = arguments[-1]
        return "sha256:different-bytes" if container == mismatch else expected[container]

    monkeypatch.setattr(provision_supervisor, "run", run)
    if mismatch:
        with pytest.raises(provision_supervisor.IsolationError, match="reviewed digest"):
            provision_supervisor.require_platform_images()
    else:
        provision_supervisor.require_platform_images()
        assert len(calls) == 4


@pytest.mark.parametrize("missing_container", ["homeassistant", "hassio_supervisor"])
def test_platform_identity_restores_missing_digest_reference_without_replacing_running_image(
    monkeypatch, missing_container
):
    """A correct retained image remains usable after its repository-digest reference disappears from the local cache."""
    pins = {"homeassistant": "core-reference", "hassio_supervisor": "supervisor-reference"}
    identities = {
        "homeassistant": "sha256:retained-core",
        "hassio_supervisor": "sha256:retained-supervisor",
    }
    monkeypatch.setattr(provision_supervisor, "CORE_IMAGE", pins["homeassistant"])
    monkeypatch.setattr(provision_supervisor, "SUPERVISOR_IMAGE", pins["hassio_supervisor"])
    references = {
        pins[container]: identity
        for container, identity in identities.items()
        if container != missing_container
    }
    calls = []

    def run(*arguments):
        """Restore only a missing local reference while independently retaining each running container's bytes."""
        calls.append(arguments)
        target = arguments[-1]
        if "pull" in arguments:
            assert target == pins[missing_container]
            references[target] = identities[missing_container]
            return "pulled"
        if "image" in arguments:
            if target not in references:
                raise subprocess.CalledProcessError(1, arguments)
            return references[target]
        return identities[target]

    monkeypatch.setattr(provision_supervisor, "run", run)
    provision_supervisor.require_platform_images()
    pulls = [command for command in calls if "pull" in command]
    assert len(pulls) == 1
    assert pulls[0][-1] == pins[missing_container]
    assert all(
        not any(operation in command for operation in ("start", "restart", "tag", "rm"))
        for command in calls
    )


@pytest.mark.parametrize("restart", ["init_process", "kernel_boot"])
def test_reused_namespace_number_cannot_retain_stale_host_network_containers(
    tmp_path, monkeypatch, restart
):
    """A fresh outer lifecycle replaces managed containers even when the kernel reuses a namespace number."""
    monkeypatch.setattr(provision_supervisor, "DEVELOPER_ROOT", tmp_path)
    monkeypatch.setattr(provision_supervisor, "SUPERVISOR_ROOT", tmp_path)
    original_stat = Path.stat
    original_read = Path.read_text
    lifecycle = {"boot": "kernel-before", "started": "100"}

    def stat(path, *arguments, **options):
        """Hold the namespace number constant while other lifecycle inputs change."""
        if path.as_posix() == "/proc/self/ns/net":
            return SimpleNamespace(st_ino=42)
        return original_stat(path, *arguments, **options)

    def read(path, *arguments, **options):
        """Supply process start ticks after a process name containing spaces and parentheses."""
        if path.as_posix() == "/proc/sys/kernel/random/boot_id":
            return lifecycle["boot"]
        if path.as_posix() == "/proc/1/stat":
            return "1 (private (init) worker) " + " ".join(
                ["S", *(["0"] * 18), lifecycle["started"], "0"]
            )
        return original_read(path, *arguments, **options)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "read_text", read)
    containers = []
    calls = []

    def run(*arguments):
        """Record owned-container replacement without touching volumes or an unrelated worker."""
        calls.append(arguments)
        if "ps" in arguments:
            return "homeassistant" if "--filter" in arguments else "\n".join(containers)
        return "started"

    monkeypatch.setattr(provision_supervisor, "run", run)
    provision_supervisor.start_supervisor()
    containers.extend(["homeassistant", "hassio_supervisor", "unrelated-worker"])
    calls.clear()
    lifecycle["started" if restart == "init_process" else "boot"] = (
        "200" if restart == "init_process" else "kernel-after"
    )
    provision_supervisor.start_supervisor()
    removed = [command for command in calls if "rm" in command]
    assert len(removed) == 1
    assert removed[0][-2:] == ("homeassistant", "hassio_supervisor")
    assert all("unrelated-worker" not in command for command in calls)
    assert all("--volumes" not in command and "--force" not in command for command in calls)
    calls.clear()
    provision_supervisor.start_supervisor()
    assert not any("rm" in command or "stop" in command for command in calls)
