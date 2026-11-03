"""Run real-Supervisor acceptance in a new private Docker project, never a personal lab."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from time import monotonic
from uuid import uuid4

from script.supervisor import dev
from script.supervisor.diagnostics import redact_diagnostics

PROCESS_TIMEOUT_SECONDS = 1200


def capture_failure(compose, results):
    """Retain bounded private-engine error diagnostics with credentials and identifiers redacted."""
    result = subprocess.run(
        [
            *compose,
            "exec",
            "-T",
            "dev",
            "docker",
            "--host",
            "unix:///run/docker.sock",
            "logs",
            "--tail",
            "200",
            "hassio_supervisor",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=dev.QUERY_TIMEOUT_SECONDS,
    )
    transcript = (getattr(result, "stdout", None) or "") + (getattr(result, "stderr", None) or "")
    (results / "supervisor-failure.log").write_text(
        redact_diagnostics(transcript) or "Supervisor diagnostic output unavailable.\n",
        encoding="utf-8",
    )


def main():
    """Provision, restart and update only this invocation's disposable Supervisor environment."""
    if os.environ.get("OEP_DEVELOPER_ENVIRONMENT") == "1":
        raise RuntimeError("Run Supervisor acceptance from the host, outside the Dev Container")
    host_engine = dev.require_local_engine()
    dev.prepare_acceptance_sources()
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=dev.ROOT, text=True, timeout=dev.QUERY_TIMEOUT_SECONDS
    ).strip()
    project = "oep-supervisor-check-" + uuid4().hex[:12]
    results = dev.ROOT / "results/ci"
    results.mkdir(parents=True, exist_ok=True)
    timings = {}
    with tempfile.TemporaryDirectory(
        prefix="supervisor-check-", dir=dev.ROOT / ".local"
    ) as temporary:
        override = Path(temporary) / "compose.yaml"
        override.write_text(
            "services:\n  dev:\n    ports: !reset []\n    cpus: 2\n    mem_limit: 4g\n",
            encoding="utf-8",
        )
        compose = [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            "compose.dev.yaml",
            "-f",
            str(override),
        ]

        def run(phase, *arguments):
            """Bound each phase and retain timings without printing private HA or app credentials."""
            started = monotonic()
            try:
                subprocess.run(
                    [*compose, *arguments],
                    cwd=dev.ROOT,
                    check=True,
                    timeout=PROCESS_TIMEOUT_SECONDS,
                )
            finally:
                timings[phase] = round(monotonic() - started, 2)
                print(f"Supervisor timing: {phase}: {timings[phase]}s", flush=True)

        execute = [
            "exec",
            "-T",
            "--env",
            f"OEP_HOST_DOCKER_ID={host_engine}",
            "--env",
            "OEP_SUPERVISOR_SMOKE=1",
            "--env",
            f"OEP_TEST_SOURCE_REVISION={revision}",
            "dev",
            "python",
            "-m",
        ]
        try:
            run("developer_image_and_engine", "up", "--detach", "--build", "dev")
            run("cold_provision", *execute, "script.supervisor.provision_supervisor")
            run(
                "initial_history_and_ingress",
                *execute,
                "script.supervisor.supervisor_smoke",
                "capture",
            )
            run("warm_provision", *execute, "script.supervisor.provision_supervisor")
            run("warm_retention", *execute, "script.supervisor.supervisor_smoke", "retained")
            run("outer_stop", "stop", "dev")
            run("outer_start", "up", "--detach", "--no-build", "dev")
            run("restart_provision", *execute, "script.supervisor.provision_supervisor")
            run("restart_retention", *execute, "script.supervisor.supervisor_smoke", "retained")
            run("supervisor_update", *execute, "script.supervisor.supervisor_smoke", "update")
            run(
                "chronological_replay",
                *execute,
                "pytest",
                "-q",
                "tests/app/forecast/test_forecast_selection.py::test_future_issued_challenger_cannot_create_historical_decision_evidence",
                "tests/app/forecast/test_forecast_selection.py::test_three_real_shadow_windows_promote_and_a_later_loss_restores_baseline",
                "tests/app/forecast/test_forecast_selection.py::test_estimated_or_missing_measurements_cannot_promote_a_model",
                "--junitxml=results/ci/supervisor-replay.xml",
            )
        finally:
            primary_error = sys.exception()
            cleanup_errors = []

            def cleanup(operation, *arguments, **options):
                """Attempt every owned-resource cleanup without replacing the failed acceptance phase."""
                try:
                    operation(*arguments, **options)
                except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
                    cleanup_errors.append(error)

            cleanup(
                (results / "supervisor-timings.json").write_text,
                json.dumps(timings, indent=2) + "\n",
                encoding="utf-8",
            )
            if primary_error is not None:
                cleanup(capture_failure, compose, results)
            # These volumes belong to this invocation, never a persistent lab.
            cleanup(
                subprocess.run,
                [*compose, "down", "--volumes", "--remove-orphans"],
                cwd=dev.ROOT,
                check=True,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            cleanup(
                subprocess.run,
                ["docker", "image", "rm", project + "-dev"],
                check=True,
                timeout=dev.QUERY_TIMEOUT_SECONDS,
            )
            if cleanup_errors:
                error = primary_error if primary_error is not None else cleanup_errors.pop(0)
                for cleanup_error in cleanup_errors:
                    error.add_note(f"Supervisor cleanup failed: {cleanup_error}")
                if primary_error is None:
                    raise error


if __name__ == "__main__":
    main()
