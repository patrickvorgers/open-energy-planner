"""Start the isolated real-Supervisor developer environment without touching other Docker projects."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from script.release.package_ha_app import package, reject_ignored, validate_sources
from script.supervisor import dev_bundle

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-p", "oep-dev", "-f", "compose.dev.yaml"]
QUERY_TIMEOUT_SECONDS = 60
ACCEPTANCE_APP_INPUTS = (
    "script/supervisor/acceptance_app.py",
    "tests/test_environment/support/sources.py",
)


def require_local_engine():
    """Reject remote Docker transports before source staging or privileged Compose operations."""
    endpoint = os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None
    if not endpoint:
        endpoint = subprocess.check_output(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            text=True,
            timeout=QUERY_TIMEOUT_SECONDS,
        ).strip()
    if not endpoint.startswith(("unix:///", "npipe:////./pipe/")):
        raise RuntimeError("Development requires a local Docker socket or Windows named pipe")
    # Keep later Docker children on this endpoint if another process changes the active context.
    os.environ["DOCKER_HOST"] = endpoint
    os.environ.pop("DOCKER_CONTEXT", None)
    return subprocess.check_output(
        ["docker", "info", "--format", "{{.ID}}"], text=True, timeout=QUERY_TIMEOUT_SECONDS
    ).strip()


def prepare():
    """Require the accepted CI package before any persistent environment operation."""
    manifest, _directory = dev_bundle.load(ROOT)
    print(f"Verified CI package: Build {manifest['build']}: Source {manifest['revision']}")


def prepare_acceptance_sources():
    """Stage a simulated-weather bootstrap only for the disposable private acceptance project."""
    validate_sources(ROOT, ACCEPTANCE_APP_INPUTS)
    reject_ignored(ROOT, ACCEPTANCE_APP_INPUTS)
    directory = dev_bundle.safe_directory(ROOT)
    with tempfile.TemporaryDirectory(prefix="source-", dir=directory) as temporary:
        app = package(Path(temporary) / "repository")
        shutil.copy2(app / "script/ha_app.py", app / "script/production_ha_app.py")
        shutil.copy2(ROOT / ACCEPTANCE_APP_INPUTS[0], app / "script/ha_app.py")
        shutil.copy2(ROOT / ACCEPTANCE_APP_INPUTS[1], app / "script/acceptance_sources.py")
        with (app / "Dockerfile").open("a", encoding="utf-8") as dockerfile:
            dockerfile.write(
                "\n# Disposable acceptance only. Release packaging never includes these fixtures.\n"
                "COPY script/production_ha_app.py script/acceptance_sources.py ./script/\n"
                "ENV OEP_SUPERVISOR_SMOKE=1\n"
            )
        destination = directory / "test-app"
        dev_bundle.require_no_links(destination)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(app), destination)


def main():
    """Build/provision the development stack or stop it without removing its persistent volumes."""
    if os.environ.get("OEP_DEVELOPER_ENVIRONMENT") == "1":
        raise RuntimeError(
            "Run python -m script.supervisor.dev from a host terminal, outside the Dev Container"
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("start", "update", "stop", "status", "prepare"),
        nargs="?",
        default="start",
    )
    parser.add_argument(
        "--bundle", type=Path, help="Original downloaded installation-bundle-amd64 ZIP"
    )
    parser.add_argument("--run-id", type=int, help="GitHub Actions run ID owning this exact ZIP")
    arguments = parser.parse_args()
    if bool(arguments.bundle) != bool(arguments.run_id):
        parser.error("Supply --bundle and --run-id together")
    if arguments.action == "update" and not arguments.bundle:
        parser.error("Updating requires --bundle and --run-id")
    if arguments.action in {"stop", "status"} and arguments.bundle:
        parser.error("Package options apply only to prepare, start or update")
    host_engine = require_local_engine()
    if arguments.action in {"stop", "status"}:
        subprocess.run(
            [*COMPOSE, "stop" if arguments.action == "stop" else "ps"], cwd=ROOT, check=True
        )
        return
    with dev_bundle.update_lock(ROOT):
        if arguments.bundle:
            dev_bundle.prepare(
                ROOT, arguments.bundle, arguments.run_id, dev_bundle.github_client(ROOT)
            )
        if arguments.action == "prepare":
            prepare()
            return
        prepare()
        subprocess.run([*COMPOSE, "up", "--detach", "--build", "dev"], cwd=ROOT, check=True)
        subprocess.run(
            [
                *COMPOSE,
                "exec",
                "-T",
                "--env",
                f"OEP_HOST_DOCKER_ID={host_engine}",
                "dev",
                "python",
                "-m",
                "script.supervisor.provision_supervisor",
            ],
            cwd=ROOT,
            check=True,
        )
        directory = ROOT / ".local" / "dev"
        directory.mkdir(parents=True, exist_ok=True)
        container = subprocess.check_output(
            [*COMPOSE, "ps", "--quiet", "dev"], cwd=ROOT, text=True, timeout=QUERY_TIMEOUT_SECONDS
        ).strip()
        target = directory / "developer.json"
        subprocess.run(
            ["docker", "cp", f"{container}:/mnt/supervisor/oep-dev/developer.json", str(target)],
            check=True,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        os.chmod(target, 0o600)
        print("Home Assistant: http://localhost:7123/app/local_open_energy_planner")
        print(f"Generated local Home Assistant login: {target}. No OEP API token is required.")


if __name__ == "__main__":
    sys.exit(main())
