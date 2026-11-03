"""Run all real HA scenarios in configured isolated processes and propagate either failure."""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic

from pytest import ExitCode

from script.ci.ha_suites import HA_WORKERS
from script.ci.pytest_suites import pytest_arguments


def run_worker(worker, arguments):
    """Write an independent JUnit report and return the actual pytest exit status."""
    started = monotonic()
    result = subprocess.run(
        [
            sys.executable,
            *pytest_arguments("ha", worker=worker, arguments=arguments),
        ],
        check=False,
    )
    print(f"HA worker {worker}: {monotonic() - started:.2f}s, exit {result.returncode}", flush=True)
    return result.returncode


def main():
    """Wait for every worker before returning failure or allowing Compose cleanup."""
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=HA_WORKERS) as workers:
        results = list(
            workers.map(lambda worker: run_worker(worker, sys.argv[1:]), range(HA_WORKERS))
        )
    passed = ExitCode.OK in results
    failed = any(result not in (ExitCode.OK, ExitCode.NO_TESTS_COLLECTED) for result in results)
    return 0 if passed and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
