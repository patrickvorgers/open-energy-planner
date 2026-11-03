"""Own pytest arguments shared by packaged-image collection and real execution."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from script.ci.ci_groups import FUNCTIONAL_DATABASE_SUITES, PYTEST_AREAS
from script.ci.ha_suites import ha_selection


def pytest_arguments(suite, *, worker=None, arguments=(), collect_only=False):
    """Keep selection and plugins identical when only collecting a suite."""
    selections = {
        **{
            suite: list(directories)
            for suite, directories in PYTEST_AREAS.items()
            if not suite.startswith("ha-")
        },
        "ci": ["tests/ci", "tests/documentation"],
        "native": [
            "--ignore=tests/custom_component/integration",
            "--ignore=tests/test_environment/integration",
            "--ignore=tests/frontend/browser",
        ],
        "documentation": [
            "tests/documentation",
            "tests/ci/test_provenance.py",
            "tests/ci/test_ci_scope.py",
        ],
        "databases": ["tests/database/contracts", "tests/database/migrations"],
        "database-contracts": ["tests/database/contracts"],
        "database-migrations": ["tests/database/migrations"],
        "performance-queries": ["tests/database/performance"],
        "browser": ["tests/frontend/browser"],
    }
    selected = selections.get(suite, [])
    if suite == "native":
        selected += ["--ignore=tests/database/performance"]
    report = {"databases": "databases", "browser": "ui"}.get(suite, suite)
    if suite == "ha" or suite.startswith("ha-"):
        selected, report = ha_selection(suite, worker)
    elif suite not in selections:
        raise KeyError(suite)
    return [
        "-m",
        "pytest",
        "-q",
        *selected,
        *(["--collect-only"] if collect_only else [f"--junitxml=results/ci/{report}.xml"]),
        "-p",
        "no:cacheprovider",
        *arguments,
    ]


def main():
    """Run a selected suite with matching collection and an app source path inherited by child processes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "suite",
        choices=(
            "native",
            "ci",
            "browser",
            *FUNCTIONAL_DATABASE_SUITES,
            "performance-queries",
            *PYTEST_AREAS,
        ),
    )
    options, arguments = parser.parse_known_args()
    return subprocess.run(
        [sys.executable, *pytest_arguments(options.suite, arguments=arguments)],
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
