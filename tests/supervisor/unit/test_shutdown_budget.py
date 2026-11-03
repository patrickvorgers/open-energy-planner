"""Keep the Supervisor stop budget compatible with real managed-app cleanup."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

import pytest

from open_energy_planner.optimization.adapters.process.worker import ProcessSolver
from open_energy_planner.runtime.entrypoint import HTTP_SHUTDOWN_TIMEOUT_SECONDS
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

ROOT = Path(__file__).resolve().parents[3]


def test_app_manifest_allows_worker_and_http_cleanup_before_platform_kill():
    """The independently specified 125-second worker and two 60-second HTTP phases fit HA's grace."""
    manifest = json.loads((ROOT / "ha_app/config.yaml").read_text(encoding="utf-8"))
    assert ProcessSolver().timeout_s == 125
    # Two additional seconds cover the existing 0.2-second join and one-second terminate join.
    # Discovery has its own default 60-second runner cleanup.
    assert (
        ProcessSolver().timeout_s + HTTP_SHUTDOWN_TIMEOUT_SECONDS + 60 + 2
        <= manifest.get("timeout", 10)
        <= 300
    )


@pytest.mark.skipif(os.name != "posix", reason="Requires actual POSIX SIGTERM delivery")
def test_sigterm_drains_planning_before_archive_close_beyond_default_grace(tmp_path):
    """A real app process survives ten-second cleanup, drains after release and preserves its original archive."""
    archive_directory = tmp_path / "data"
    repository = SqlPlanRepository(archive_directory)
    SqlDocuments(repository.engine, "planning").write(
        "retained-synthetic-history.json", {"original": [1, 2, 3]}
    )
    repository.close()
    ready_marker = tmp_path / "ready"
    stopping_marker = tmp_path / "stopping"
    release_marker = tmp_path / "release"
    event_log_path = tmp_path / "events"
    script = tmp_path / "managed_shutdown.py"
    script.write_text(
        '''"""Hold synthetic work while actual managed-app signal cleanup drains."""
import asyncio
import sys
from pathlib import Path
from aiohttp import web
from open_energy_planner.app import create_app
from open_energy_planner.optimization.domain import SolveResult, Status
from open_energy_planner.planning import PlanningCoordinator
from open_energy_planner.runtime.maintenance import create_managed_app
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

archive_directory, ready_marker, stopping_marker, release_marker, event_log_path = map(Path, sys.argv[1:])
original_planner_close = PlanningCoordinator.close
original_archive_close = SqlPlanRepository.close

def record(event):
    """Retain only synthetic lifecycle ordering, never private state."""
    with event_log_path.open("a", encoding="utf-8") as output:
        output.write(event + "\\n")

class HeldSolver:
    """Keep a valid operation pending until the parent explicitly releases it."""
    async def solve(self, problem):
        """Make premature cancellation fail instead of pretending the work drained."""
        ready_marker.touch()
        await asyncio.sleep(0)
        while not release_marker.exists():
            await asyncio.sleep(0.01)
        record("solver.finished")
        return SolveResult(Status.ERROR, None, "synthetic-release", 0, 0, None)

async def close_planner(coordinator):
    """Observe the unchanged production coordinator's cleanup."""
    stopping_marker.touch()
    await original_planner_close(coordinator)
    record("planner.closed")

def close_archive(repository):
    """Observe archive disposal after the coordinator drains."""
    record("archive.closed")
    original_archive_close(repository)

PlanningCoordinator.close = close_planner
SqlPlanRepository.close = close_archive
web.run_app(create_managed_app(archive_directory, "synthetic-shutdown-token-0123456789",
    factory=create_app, solver=HeldSolver()), host="127.0.0.1", port=0, print=None)
record("process.finished")
''',
        encoding="utf-8",
    )
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    with (tmp_path / "process.log").open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                str(script),
                *map(
                    str,
                    (
                        archive_directory,
                        ready_marker,
                        stopping_marker,
                        release_marker,
                        event_log_path,
                    ),
                ),
            ],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            for marker in (ready_marker, stopping_marker):
                deadline = monotonic() + 15
                while not marker.exists():
                    assert process.poll() is None, "App exited before lifecycle observation"
                    assert monotonic() < deadline, "App never reached the observed lifecycle phase"
                    sleep(0.01)
                if marker == ready_marker:
                    process.send_signal(signal.SIGTERM)
            sleep(10.1)
            assert process.poll() is None
            assert not event_log_path.exists()
            release_marker.touch()
            assert process.wait(timeout=20) == 0
        finally:
            release_marker.touch()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert event_log_path.read_text(encoding="utf-8").splitlines() == [
        "solver.finished",
        "planner.closed",
        "archive.closed",
        "process.finished",
    ]
    repository = SqlPlanRepository(archive_directory)
    try:
        assert SqlDocuments(repository.engine, "planning").read(
            "retained-synthetic-history.json"
        ) == {"original": [1, 2, 3]}
    finally:
        repository.close()
