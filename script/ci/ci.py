"""Run the complete isolated container suite locally or on GitHub Actions."""

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from uuid import uuid4

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
scope = importlib.import_module("script.ci.ci_scope")
check_images = importlib.import_module("script.ci.check_images").check_images
completion = importlib.import_module("script.ci.check_suite_completion")

EARLY_CHECK_WORKERS = 2
DATABASE_CHECK_WORKERS = 1
completed_suites = set()
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--stage",
    choices=("all", "preflight", "tests"),
    default="all",
    help="Run both stages locally or separate image loading and execution in Actions.",
)
parser.add_argument(
    "--documentation-only",
    action="store_true",
    help="Run focused documentation gates. CI validates eligibility separately.",
)
parser.add_argument(
    "--prebuilt",
    action="store_true",
    help="Use images built by Actions Bake. Require its isolated COMPOSE_PROJECT_NAME.",
)
parser.add_argument(
    "--profile",
    default="full",
    help="Run suites selected by the trusted base policy. Defaults to full.",
)
arguments = parser.parse_args()
if arguments.documentation_only and arguments.profile != "full":
    parser.error("Choose --documentation-only or --profile")
profile = "documentation" if arguments.documentation_only else arguments.profile
documentation_only = profile == "documentation"
suites = scope.selected_suites(profile)
focused = profile.startswith("selected:") or scope.matrix_profile(profile) == "ci"
database_suites = suites & set(scope.FUNCTIONAL_DATABASE_SUITES)
ha_selected = "ha" in suites or any(suite.startswith("ha-") for suite in suites)
app_selected = bool(suites & {"ha", "ha-controls", "ha-solver", "provisioning"})
database_selected = bool(database_suites or "performance-queries" in suites)
phase_times = []
pipeline_started = monotonic()
timing_report = Path(
    "results/ci/phase-times-preflight.json"
    if arguments.stage == "preflight"
    else "results/ci/phase-times.json"
)
project = f"oep-check-{uuid4().hex[:12]}"
if arguments.prebuilt:
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    if not re.fullmatch(r"oep-check-[0-9]+-[0-9]+", project):
        parser.error("--prebuilt requires COMPOSE_PROJECT_NAME=oep-check-<run id>-<attempt>")
os.chdir(root)
Path("results/ci").mkdir(parents=True, exist_ok=True)
if arguments.stage != "preflight":
    completion.REPORT.unlink(missing_ok=True)
    for reports in completion.junit_reports(profile).values():
        for report in reports:
            report.unlink(missing_ok=True)
subprocess.run([sys.executable, "script/ci/check_provenance.py"], check=True)
compose = ["docker", "compose", "-p", project, "-f", "compose.yaml"]


def run(*args, suite=None):
    """Run a checked Compose operation against this CI invocation's uniquely named project."""
    started = monotonic()
    succeeded = False
    try:
        subprocess.run([*compose, *args], check=True)
        succeeded = True
        if suite is not None:
            completed_suites.add(suite)
    finally:
        elapsed = monotonic() - started
        phase_times.append(
            {
                "command": list(args),
                "suite": suite,
                "start_seconds": started - pipeline_started,
                "duration_seconds": elapsed,
                "success": succeeded,
            }
        )
        print(f"CI timing: {' '.join(args)}: {elapsed:.2f}s", flush=True)


def run_database_suites():
    """Run selected functional groups sequentially on the same disposable backend services."""
    for suite in sorted(database_suites):
        if suite == "databases":
            run("run", "--rm", "db-tests", suite=suite)
        else:
            run("run", "--rm", "db-tests", "-m", "script.ci.pytest_suites", suite, suite=suite)
    if "performance-queries" in suites:
        run(
            "run",
            "--rm",
            "db-tests",
            "-m",
            "script.ci.pytest_suites",
            "performance-queries",
            suite="performance-queries",
        )


try:
    targets = ["native"]
    if app_selected:
        targets.append("app")
    if ha_selected:
        targets.append("ha-tests")
    if "ha" in suites or "provisioning" in suites:
        targets.append("provision-tests")
    if "browser" in suites:
        targets.append("ui-tests")
    if database_selected:
        targets.append("db-tests")
    if arguments.prebuilt:
        subprocess.run(
            ["docker", "image", "inspect", *[f"{project}-{target}" for target in targets]],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    else:
        run("--profile", "test", "build", *targets)
    if arguments.stage != "tests":
        check_images(compose, project, profile, suites)
    if arguments.stage == "preflight":
        pass
    elif documentation_only:
        run(
            "run",
            "--rm",
            "native",
            "script/ci/check_native.py",
            "--documentation-only",
            suite="documentation",
        )
    elif focused:
        run("run", "--rm", "native", "script/ci/check_native.py", "--ci-only", suite="ci")
        if "unit-app" in suites or suites & scope.APP_UNIT_SUITES:
            run("run", "--rm", "native", "script/app/smoke_app.py")
            run("run", "--rm", "native", "script/app/debug_scenario.py")
        for suite in sorted(suites):
            if suite.startswith("unit-"):
                run("run", "--rm", "native", "-m", "script.ci.pytest_suites", suite, suite=suite)
        if "browser" in suites:
            run("run", "--rm", "ui-tests", suite="browser")
        if "ha-contracts" in suites or "ha" in suites:
            run(
                "run",
                "--no-deps",
                "--rm",
                "ha-tests",
                "-m",
                "script.ci.pytest_suites",
                "ha-contracts",
                suite="ha-contracts",
            )
        if database_selected:
            run("up", "--detach", "--wait", "postgres-test", "mariadb-test", "mysql-test")
        with ThreadPoolExecutor(max_workers=DATABASE_CHECK_WORKERS) as background:
            database_checks = background.submit(run_database_suites) if database_selected else None
            if app_selected:
                run("up", "--no-build", "--detach", "--wait", "app")
            if "ha" in suites:
                run(
                    "run",
                    "--rm",
                    "ha-tests",
                    "-m",
                    "script.ci.check_ha",
                    "--ignore=tests/custom_component/integration/contracts",
                    suite="ha",
                )
                run("run", "--rm", "provision-tests", suite="provisioning")
            for suite in sorted(suites):
                if suite.startswith("ha-") and suite != "ha-contracts":
                    run(
                        "run",
                        "--no-deps",
                        "--rm",
                        "ha-tests",
                        "-m",
                        "script.ci.pytest_suites",
                        suite,
                        suite=suite,
                    )
            if "provisioning" in suites:
                run("run", "--rm", "provision-tests", suite="provisioning")
            if database_checks is not None:
                database_checks.result()
    else:
        # Separate stores/reports fit within 4 GiB, before any DB or HA services start.
        with ThreadPoolExecutor(max_workers=EARLY_CHECK_WORKERS) as early:
            native_arguments = (
                ("script/ci/check_native.py", "--without-database-contracts")
                if database_suites
                else ()
            )
            native_checks = early.submit(
                run, "run", "--rm", "native", *native_arguments, suite="native"
            )
            browser_checks = (
                early.submit(run, "run", "--rm", "ui-tests", suite="browser")
                if "browser" in suites
                else None
            )
            native_checks.result()
            if browser_checks is not None:
                browser_checks.result()
        if "ha" in suites:
            run(
                "run",
                "--no-deps",
                "--rm",
                "ha-tests",
                "-m",
                "script.ci.pytest_suites",
                "ha-contracts",
                suite="ha-contracts",
            )
        if database_selected:
            run("up", "--detach", "--wait", "postgres-test", "mariadb-test", "mysql-test")
        # Database contracts and HA/browser fixtures use separate stores and report files.
        with ThreadPoolExecutor(max_workers=DATABASE_CHECK_WORKERS) as background:
            database_checks = background.submit(run_database_suites) if database_selected else None
            if "ha" in suites:
                run("up", "--no-build", "--detach", "--wait", "app")
                run(
                    "run",
                    "--rm",
                    "ha-tests",
                    "-m",
                    "script.ci.check_ha",
                    "--ignore=tests/custom_component/integration/contracts",
                    suite="ha",
                )
                run("run", "--rm", "provision-tests", suite="provisioning")
            if database_checks is not None:
                database_checks.result()
finally:
    try:
        timing_report.write_text(
            json.dumps(
                {
                    "profile": profile,
                    "stage": arguments.stage,
                    "phases": phase_times,
                    "elapsed_seconds": monotonic() - pipeline_started,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        try:
            subprocess.run([*compose, "logs", "--no-color"], check=False)
        finally:
            subprocess.run(
                [*compose, "--profile", "test", "down", "--volumes", "--remove-orphans"],
                check=False,
            )

if "supervisor" in suites and arguments.stage != "preflight":
    started = monotonic()
    try:
        subprocess.run(
            [sys.executable, "-m", "script.supervisor.check_supervisor"], cwd=root, check=True
        )
        completed_suites.add("supervisor")
    finally:
        phase_times.append(
            {
                "suite": "supervisor",
                "start_seconds": started - pipeline_started,
                "duration_seconds": monotonic() - started,
                "success": "supervisor" in completed_suites,
            }
        )
        Path("results/ci/phase-times.json").write_text(
            json.dumps(
                {
                    "profile": profile,
                    "phases": phase_times,
                    "elapsed_seconds": monotonic() - pipeline_started,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

if arguments.stage != "preflight":
    completion.write_completion(profile, completed_suites)
