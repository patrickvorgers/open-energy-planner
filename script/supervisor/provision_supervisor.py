"""Provision the private oep-dev Docker engine through the actual Supervisor and HA APIs.

Source copies belong only to the local app build context and custom integrations.
Runtime settings, application data and Recorder history are never replaced.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
from http.client import HTTPException
from pathlib import Path
from time import monotonic, sleep
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen

from script.home_assistant.bootstrap import provision_ha
from script.provisioning import device_examples as provision_device_examples
from script.provisioning import sample_settings as provision_sample_settings
from script.release.package_ha_app import ROOT, validate_sources
from script.release.verify_deployment import verify
from script.supervisor import dev_bundle
from script.supervisor.platform_images import platform_pin

SUPERVISOR_IMAGE, SUPERVISOR_VERSION = platform_pin("supervisor")
CORE_IMAGE, CORE_VERSION = platform_pin("core")
APP_SLUG = "local_open_energy_planner"
SUPERVISOR_ROOT = Path("/mnt/supervisor")
DEVELOPER_ROOT = SUPERVISOR_ROOT / "oep-dev"
PLATFORM_READY_TIMEOUT_SECONDS = 900
APP_READY_TIMEOUT_SECONDS = 180
PROCESS_TIMEOUT_SECONDS = 900
POLL_SECONDS = 2
APP_REQUEST_TIMEOUT_SECONDS = 5
MAX_CLI_DIAGNOSTIC_CHARACTERS = 4096
STORE_RELOAD_TIMEOUT_SECONDS = 180
MAX_STORE_RESPONSE_BYTES = 65536
PRIVATE_SUPERVISOR_ADDRESS = "172.30.32.2"
DOCKER = ["docker", "--host", "unix:///run/docker.sock"]


class IsolationError(RuntimeError):
    """Reject a permanent environment ownership failure before provisioning can mutate anything."""


def run(*arguments):
    """Capture subprocess output privately. Administration credentials must never reach logs."""
    return subprocess.check_output(
        list(arguments), text=True, stderr=subprocess.PIPE, timeout=PROCESS_TIMEOUT_SECONDS
    ).strip()


def require_private_engine():
    """Reject a host shell, remote Docker context or mounted host engine before any mutation."""
    if (
        os.environ.get("OEP_DEVELOPER_ENVIRONMENT") != "1"
        or os.environ.get("DEVCONTAINER") != "1"
        or not Path("/.dockerenv").exists()
        or not SUPERVISOR_ROOT.is_mount()
    ):
        raise IsolationError("Provisioning requires the isolated oep-dev container")
    engine = json.loads(run(*DOCKER, "info", "--format", "{{json .}}"))
    if engine["Name"] != socket.gethostname() or engine["ID"] == os.environ.get(
        "OEP_HOST_DOCKER_ID"
    ):
        raise IsolationError("Refusing a Docker engine outside the development container")
    return engine["ID"]


def wait_for(operation, description, *, timeout=PLATFORM_READY_TIMEOUT_SECONDS):
    """Bound readiness polling without treating a successful container start as application readiness."""
    deadline = monotonic() + timeout
    last_error = None
    while monotonic() < deadline:
        try:
            result = operation()
            if result:
                return result
        except IsolationError:
            raise
        except (subprocess.SubprocessError, ValueError, OSError, RuntimeError) as error:
            last_error = error
        sleep(POLL_SECONDS)
    raise RuntimeError(f"Timed out waiting for {description}") from last_error


class NoSupervisorRedirects(HTTPRedirectHandler):
    """Never forward the private Supervisor credential to a redirected endpoint."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        """Reject every redirect instead of constructing an authenticated follow-up request."""
        return


def reload_acceptance_store():
    """Reload only the disposable Supervisor store without the CLI's fixed 30-second timeout."""
    if os.environ.get("OEP_SUPERVISOR_SMOKE") != "1":
        raise IsolationError("Direct store reload requires disposable Supervisor acceptance")
    require_private_engine()
    try:
        cli, supervisor = json.loads(run(*DOCKER, "inspect", "hassio_cli", "hassio_supervisor"))
        if (
            cli["Name"] != "/hassio_cli"
            or supervisor["Name"] != "/hassio_supervisor"
            or supervisor["NetworkSettings"]["Networks"]["hassio"]["IPAddress"]
            != PRIVATE_SUPERVISOR_ADDRESS
        ):
            raise ValueError("Unexpected private Supervisor identity")
        tokens = [
            value.partition("=")[2]
            for value in cli["Config"]["Env"]
            if value.startswith("SUPERVISOR_TOKEN=")
        ]
        if len(tokens) != 1 or not re.fullmatch(r"[A-Za-z0-9._~-]+", tokens[0]):
            raise ValueError("Invalid private Supervisor credential")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        raise RuntimeError("Cannot verify private Supervisor store access") from None
    request = Request(
        f"http://{PRIVATE_SUPERVISOR_ADDRESS}/store/reload",
        method="POST",
        headers={"Authorization": f"Bearer {tokens[0]}"},
    )
    opener = build_opener(ProxyHandler({}), NoSupervisorRedirects())
    try:
        with opener.open(request, timeout=STORE_RELOAD_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise ValueError("Unsuccessful HTTP response")
            payload = response.read(MAX_STORE_RESPONSE_BYTES + 1)
        if len(payload) > MAX_STORE_RESPONSE_BYTES:
            raise ValueError("Oversized Supervisor response")
        result = json.loads(payload)
        if not isinstance(result, dict) or result.get("result") != "ok":
            raise ValueError("Unsuccessful Supervisor response")
    except (OSError, ValueError, HTTPException):
        # Response bodies and transport exceptions may contain the private credential.
        raise RuntimeError("Private Supervisor store reload failed or timed out") from None
    return result.get("data", {})


def ha(*arguments):
    """Require a successful actual Supervisor response, without printing potentially private fields."""
    from script.supervisor.diagnostics import redact_diagnostics

    if arguments == ("store", "reload") and os.environ.get("OEP_SUPERVISOR_SMOKE") == "1":
        return reload_acceptance_store()
    operation = " ".join(arguments[:2])
    try:
        result = json.loads(run("ha", *arguments, "--raw-json"))
    except subprocess.CalledProcessError as error:
        details = redact_diagnostics((error.output or "") + "\n" + (error.stderr or ""))[
            :MAX_CLI_DIAGNOSTIC_CHARACTERS
        ]
        # CLI arguments can contain private options. Retain only the action and redacted response.
        raise RuntimeError(
            f"Supervisor command failed ({operation}, exit {error.returncode}): {details}"
        ) from None
    if result.get("result") != "ok":
        details = redact_diagnostics(json.dumps(result))[:MAX_CLI_DIAGNOSTIC_CHARACTERS]
        raise RuntimeError(f"Supervisor operation failed ({operation}): {details}")
    return result.get("data", {})


def public_tree(source, destination):
    """Replace only an owned source directory. Public code remains readable after privilege dropping."""
    allowed_parents = (
        SUPERVISOR_ROOT / "apps/local",
        SUPERVISOR_ROOT / "homeassistant/custom_components",
    )
    if destination.parent not in allowed_parents:
        raise ValueError("Refusing to replace a directory outside the development source locations")
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise ValueError("Development source destinations cannot be symbolic links")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for item in (destination, *destination.rglob("*")):
        item.chmod(0o755 if item.is_dir() else 0o644)


def source_digest(directory):
    """Hash source paths and bytes so warm starts avoid needless rebuilds without missing deletions."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def prepare_sources(previous):
    """Stage an accepted CI pair, with candidate source staging confined to disposable acceptance."""
    if os.environ.get("OEP_SUPERVISOR_SMOKE") == "1":
        app = ROOT / ".local/dev/test-app"
        integration = ROOT / "custom_components/open_energy_planner"
    else:
        _manifest, bundle = dev_bundle.load(ROOT)
        app, integration = bundle / "app", bundle / "integration"
    validate_sources(
        ROOT, (app.relative_to(ROOT), integration.relative_to(ROOT), "test_environment")
    )
    local_apps = SUPERVISOR_ROOT / "apps/local"
    local_apps.mkdir(parents=True, exist_ok=True)
    core = SUPERVISOR_ROOT / "homeassistant"
    core.mkdir(exist_ok=True)
    integrations = core / "custom_components"
    integrations.mkdir(exist_ok=True)
    if not (app / "Dockerfile").is_file():
        raise RuntimeError(
            "Prepare development sources first with python -m script.supervisor.dev prepare"
        )
    expected = {
        "app": source_digest(app),
        "integration": source_digest(integration),
        "fixture": source_digest(ROOT / "test_environment/oep_test_fixture"),
    }
    if expected["app"] != previous.get("app"):
        public_tree(app, local_apps / "open-energy-planner")
    for name, key, source in (
        ("open_energy_planner", "integration", integration),
        ("oep_test_fixture", "fixture", ROOT / "test_environment/oep_test_fixture"),
    ):
        if expected[key] != previous.get(key):
            public_tree(source, integrations / name)
    # Only the first start supplies sample configuration. Existing intent is authoritative.
    for name, source in (
        ("configuration.yaml", ROOT / "test_environment/configuration.yaml"),
        ("oep-test.yaml", ROOT / "test_environment/dashboard.yaml"),
    ):
        destination = core / name
        if not destination.exists():
            shutil.copyfile(source, destination)
    automation = core / "automations.yaml"
    if not automation.exists():
        automation.write_text("[]\n", encoding="utf-8")
    return expected


def prepare_core_image():
    """Seed only a fresh Supervisor Core configuration from the tested official image digest.

    Existing platform configuration remains authoritative and is never upgraded.
    Supervisor still owns container creation, startup, onboarding and its APIs.
    """
    configuration = SUPERVISOR_ROOT / "homeassistant.json"
    if configuration.exists():
        return
    run(*DOCKER, "pull", CORE_IMAGE)
    tag = CORE_IMAGE.split("@", 1)[0]
    run(*DOCKER, "tag", CORE_IMAGE, tag)
    configuration.write_text(
        json.dumps(
            {"image": tag.rsplit(":", 1)[0], "version": CORE_VERSION, "override_image": True},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def require_platform_images():
    """Reject running image bytes that differ from either reviewed platform digest without upgrading anything."""
    for container, image in (
        ("homeassistant", CORE_IMAGE),
        ("hassio_supervisor", SUPERVISOR_IMAGE),
    ):
        actual = run(*DOCKER, "inspect", "--format", "{{.Image}}", container)
        try:
            expected = run(*DOCKER, "image", "inspect", "--format", "{{.Id}}", image)
        except subprocess.CalledProcessError:
            # Restoring a digest reference does not replace the running container.
            run(*DOCKER, "pull", image)
            expected = run(*DOCKER, "image", "inspect", "--format", "{{.Id}}", image)
        if actual != expected:
            raise IsolationError(
                "Running platform image differs from the reviewed digest: " + container
            )


def start_supervisor():
    """Start the pinned official Supervisor in the inner engine with its native host-service mounts."""
    containers = run(*DOCKER, "ps", "--all", "--format", "{{.Names}}").splitlines()
    namespace = str(Path("/proc/self/ns/net").stat().st_ino)
    kernel_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    init_started = Path("/proc/1/stat").read_text().rsplit(")", 1)[1].split()[19]
    network_identity = f"{kernel_boot}:{init_started}:{namespace}"
    marker = DEVELOPER_ROOT / "engine-namespace"
    previous_identity = marker.read_text() if marker.exists() else None
    if containers and network_identity != previous_identity:
        # Namespace numbers can be reused after an outer restart, so include the kernel and init lifecycle.
        owned = run(
            *DOCKER, "ps", "--all", "--filter", "label=supervisor_managed", "--format", "{{.Names}}"
        ).splitlines()
        if "hassio_supervisor" in containers and "hassio_supervisor" not in owned:
            owned.append("hassio_supervisor")
        if owned:
            run(*DOCKER, "stop", "--timeout", "30", *owned)
            run(*DOCKER, "rm", *owned)
        containers = [name for name in containers if name not in owned]
    marker.write_text(network_identity, encoding="utf-8")
    if "hassio_supervisor" in containers:
        running = run(*DOCKER, "ps", "--format", "{{.Names}}").splitlines()
        if "hassio_supervisor" in running:
            return
    # Bootstrap can schedule an update before the Supervisor API accepts options.
    updater = SUPERVISOR_ROOT / "updater.json"
    if updater.is_symlink():
        raise IsolationError("Supervisor updater configuration must not be a symbolic link")
    configuration = json.loads(updater.read_text(encoding="utf-8")) if updater.exists() else {}
    configuration["auto_update"] = False
    updater.write_text(json.dumps(configuration, indent=2) + "\n", encoding="utf-8")
    if "hassio_supervisor" in containers:
        run(*DOCKER, "start", "hassio_supervisor")
        return
    mounts = (
        "/run/docker.sock:/run/docker.sock:rw",
        "/run/dbus:/run/dbus:ro",
        "/run/supervisor:/run/os:rw",
        "/run/udev:/run/udev:ro",
        "/mnt/supervisor:/data:rw",
        "/etc/machine-id:/etc/machine-id:ro",
    )
    arguments = [
        *DOCKER,
        "run",
        "--detach",
        "--name",
        "hassio_supervisor",
        "--privileged",
        "--security-opt",
        "seccomp=unconfined",
        "--security-opt",
        "apparmor=hassio-supervisor",
    ]
    for mount in mounts:
        arguments.extend(("--volume", mount))
    for setting in (
        "SUPERVISOR_SHARE=/mnt/supervisor",
        "SUPERVISOR_NAME=hassio_supervisor",
        "SUPERVISOR_MACHINE=qemux86-64",
        "SUPERVISOR_SYSTEMD_JOURNAL_GATEWAYD_URL=http://172.30.32.1:19531/",
    ):
        arguments.extend(("--env", setting))
    run(*arguments, SUPERVISOR_IMAGE)


def read_app(app_url, token, route="setup"):
    """Read the private app API with a bounded request timeout, without exposing its credential."""
    request = Request(app_url + "/v1/" + route, headers={"Authorization": "Bearer " + token})
    with urlopen(request, timeout=APP_REQUEST_TIMEOUT_SECONDS) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise TypeError("Unexpected development app response")
    return result


def verify_installed_package():
    """Require loaded app/integration CI identities before publishing persistent update success."""
    manifest, _bundle = dev_bundle.load(ROOT)
    app = ha("apps", "info", APP_SLUG)
    token = run(*DOCKER, "exec", f"app_{APP_SLUG}", "cat", "/data/administration-token")
    status = read_app(f"http://{app['ip_address']}:8099", token, "version")
    verify(status, manifest)
    if status["app"].get("revision") != manifest["revision"]:
        raise ValueError("Loaded app source differs from the CI bundle")
    return status


async def finish_setup():
    """Initialize missing sample settings before requiring the final healthy Supervisor projection."""
    app = ha("apps", "info", APP_SLUG)
    app_url = f"http://{app['ip_address']}:8099"
    token = wait_for(
        lambda: run(*DOCKER, "exec", f"app_{APP_SLUG}", "cat", "/data/administration-token"),
        "the installed app's private administration secret",
        timeout=APP_READY_TIMEOUT_SECONDS,
    )
    wait_for(
        lambda: read_app(app_url, token),
        "the installed app's authenticated HTTP API",
        timeout=APP_READY_TIMEOUT_SECONDS,
    )
    # Initial configuration clears operational not-ready states without weakening ownership checks.
    await provision_sample_settings.provision(app_url=app_url, token=token)
    journal = DEVELOPER_ROOT / "example-provisioning.json"
    await provision_device_examples.provision(app_url=app_url, token=token, journal_path=journal)
    os.environ.update(
        OEP_DEV_ONBOARD_ONLY="false", OEP_DEV_AUTOMATIC="true", OEP_DEV_EXPECT_ACTIVE="false"
    )
    async with provision_device_examples.product_requests(
        app_url=app_url, token=token
    ) as example_request:
        await provision_ha.provision(examples_journal=journal, example_request=example_request)
    setup = read_app(app_url, token)
    if not setup.get("annual_estimate", {}).get("days"):
        raise RuntimeError("Historical training inputs are not ready")
    print(f"Simulated training history: {setup['annual_estimate']['days']} complete days.")


def main():
    """Provision the isolated local app once, preserving its storage and skipping unchanged rebuilds."""
    import fcntl

    wait_for(require_private_engine, "the private Docker engine")
    print("Private development Docker engine verified.", flush=True)
    DEVELOPER_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (DEVELOPER_ROOT / "provision.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = DEVELOPER_ROOT / "source.json"
        previous = json.loads(marker.read_text()) if marker.exists() else {}
        expected = prepare_sources(previous)
        prepare_core_image()
        start_supervisor()
        supervisor = wait_for(lambda: ha("supervisor", "info"), "Supervisor")
        print("Actual Supervisor is ready.", flush=True)
        if supervisor["version"] != SUPERVISOR_VERSION:
            raise RuntimeError(
                "Unexpected Supervisor version. No automatic platform change is allowed"
            )
        ha("supervisor", "options", "--auto-update=false")

        def installed_core():
            """Wait through the first-boot landing page before checking the supported Core version."""
            core = ha("core", "info")
            return core if core.get("version") not in (None, "landingpage") else None

        core = wait_for(installed_core, "Home Assistant")
        if core["version"] != CORE_VERSION:
            raise RuntimeError("Unexpected Core version. Review the development platform pins")
        wait_for(lambda: ha("info").get("state") == "running", "Supervisor startup completion")
        require_platform_images()
        # A fresh Core already reads the sources staged before Supervisor started it.
        if previous and expected != previous:
            ha("core", "restart")
        os.environ.update(
            OEP_DEV_HA_URL="http://homeassistant",
            OEP_DEV_STATE=str(DEVELOPER_ROOT / "developer.json"),
            OEP_DEV_ONBOARD_ONLY="true",
        )
        asyncio.run(provision_ha.provision())
        print("Home Assistant onboarding is ready.", flush=True)
        installed_apps = ha("apps").get("addons", [])
        app_installed = any(app_record["slug"] == APP_SLUG for app_record in installed_apps)
        app_changed = expected.get("app") != previous.get("app")
        if not app_installed or app_changed:
            ha("store", "reload")
        if not app_installed:
            ha("apps", "install", APP_SLUG)
        elif app_changed:
            ha("apps", "rebuild", APP_SLUG, "--force")
        if ha("apps", "info", APP_SLUG).get("state") != "started":
            ha("apps", "start", APP_SLUG)
        print("OEP is installed through the actual Supervisor.", flush=True)
        wait_for(
            lambda: ha("apps", "info", APP_SLUG).get("state") == "started",
            "the installed OEP app",
            timeout=APP_READY_TIMEOUT_SECONDS,
        )
        asyncio.run(finish_setup())
        if os.environ.get("OEP_SUPERVISOR_SMOKE") != "1":
            wait_for(
                verify_installed_package,
                "the installed CI build identities",
                timeout=APP_READY_TIMEOUT_SECONDS,
            )
        # Core onboarding can request an update independently of the Supervisor schedule.
        require_platform_images()
        marker.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
        print(
            "Real Supervisor developer environment ready. Existing settings and history retained."
        )


if __name__ == "__main__":
    main()
