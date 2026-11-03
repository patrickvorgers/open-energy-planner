"""Prepare and clean only the current Actions test project on a personal Docker host."""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

from script.supervisor.dev import QUERY_TIMEOUT_SECONDS, require_local_engine

ROOT = Path(__file__).resolve().parents[2]
IMAGE_SERVICES = ("app", "native", "ha-tests", "ui-tests", "provision-tests", "db-tests")
CLEANUP_TIMEOUT_SECONDS = 120


def prepare():
    """Require Linux orchestration and a local Linux x64 engine without changing its configuration."""
    if platform.system() != "Linux" or sys.version_info < (3, 12):
        raise RuntimeError("Personal CI requires Linux with Python 3.12 or newer")
    require_local_engine()
    engine = json.loads(
        subprocess.check_output(
            ["docker", "info", "--format", "{{json .}}"],
            text=True,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
    )
    if engine.get("OSType") != "linux" or engine.get("Architecture") not in {"x86_64", "amd64"}:
        raise RuntimeError("Personal CI requires Linux x64 Docker containers")
    subprocess.run(["docker", "compose", "version"], check=True, timeout=QUERY_TIMEOUT_SECONDS)
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as destination:
        destination.write(f"DOCKER_HOST={os.environ['DOCKER_HOST']}\nDOCKER_CONTEXT=\n")


def cleanup():
    """Remove only this run's containers, volumes and uniquely tagged images without global pruning."""
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    if not re.fullmatch(r"oep-check-[0-9]+-[0-9]+", project):
        raise RuntimeError("Refusing cleanup without an exact Actions project identity")
    require_local_engine()
    subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            "compose.yaml",
            "--profile",
            "test",
            "down",
            "--volumes",
            "--remove-orphans",
        ],
        cwd=ROOT,
        check=True,
        timeout=CLEANUP_TIMEOUT_SECONDS,
    )
    for service in IMAGE_SERVICES:
        image = f"{project}-{service}"
        present = subprocess.run(
            ["docker", "image", "inspect", image],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        if present.returncode == 0:
            subprocess.run(
                ["docker", "image", "rm", image], check=True, timeout=QUERY_TIMEOUT_SECONDS
            )


def main():
    """Expose only preparation and owned-project cleanup to the shared Actions workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "cleanup"))
    arguments = parser.parse_args()
    if arguments.action == "prepare":
        prepare()
    else:
        cleanup()


if __name__ == "__main__":
    main()
