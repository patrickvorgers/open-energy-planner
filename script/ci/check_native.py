"""Shared native checks for Dev Container and CI. No network or HA dependency."""

import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
pytest_arguments = importlib.import_module("script.ci.pytest_suites").pytest_arguments
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--ci-only",
    action="store_true",
    help="Run lint, formatting and CI policy regressions without app scenarios.",
)
parser.add_argument(
    "--quick",
    action="store_true",
    help="Run Ruff and formatting without Docker or app dependencies.",
)
parser.add_argument(
    "--without-database-contracts",
    action="store_true",
    help="Database contracts run separately on all four backends.",
)
parser.add_argument(
    "--documentation-only",
    action="store_true",
    help="Keep lint, formatting and focused documentation/routing regressions.",
)
arguments = parser.parse_args()
documentation_only = arguments.documentation_only
if sum((arguments.quick, documentation_only, arguments.ci_only)) > 1:
    parser.error("Choose quick, CI policy or documentation checks")
os.chdir(root)
Path("results/ci").mkdir(parents=True, exist_ok=True)
commands = (
    [
        "-m",
        "ruff",
        "check",
        "src",
        "custom_components",
        "tests",
        "script",
        "tests/frontend",
        "test_environment",
    ],
    [
        "-m",
        "ruff",
        "format",
        "--check",
        "src",
        "custom_components",
        "tests",
        "script",
        "tests/frontend",
        "test_environment",
    ],
    pytest_arguments(
        "native",
        arguments=("--ignore=tests/database",) if arguments.without_database_contracts else (),
    ),
    ["script/app/smoke_app.py"],
    ["script/app/debug_scenario.py"],
)
if arguments.quick:
    commands = commands[:2]
elif documentation_only:
    commands = commands[:2] + (pytest_arguments("documentation"),)
elif arguments.ci_only:
    commands = commands[:2] + (pytest_arguments("ci"),)
for command in commands:
    subprocess.run([sys.executable, *command], check=True, env={**os.environ, "PYTHONPATH": "src"})
