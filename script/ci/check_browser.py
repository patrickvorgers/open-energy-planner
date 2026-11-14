"""Run bounded browser containers, join failures and retain independent worker evidence."""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic

from script.ci.browser_suites import (
    BROWSER_WORKERS_PER_MACHINE,
    collections,
    combine_reports,
    execution_reports,
    worker_directory,
)
from script.ci.pytest_suites import pytest_arguments


def worker_arguments(directory, worker, *, collect_only=False):
    """Describe one image and exact pytest selection without changing the worker's output files."""
    location = worker_directory(directory, worker).resolve()
    return [
        "run",
        "--no-deps",
        "--pull",
        "never",
        "--rm",
        "--volume",
        f"{location}:/workspace/results/ci",
        "--entrypoint",
        "python",
        "ui-worker",
        *pytest_arguments("browser", worker=worker, collect_only=collect_only),
    ]


def prepare_worker_reports(directory, worker, *, collect_only=False):
    """Create the private mount and clear only the evidence about to be produced by a real launch."""
    location = worker_directory(directory, worker).resolve()
    location.mkdir(parents=True, exist_ok=True)
    stale = ("browser-collected.json",) if collect_only else ("browser-executed.json", "ui.xml")
    for name in stale:
        (location / name).unlink(missing_ok=True)


def run_workers(compose, *, machine=None):
    """Allow completion only after every worker has exited and its actual cases are verified."""
    directory = Path("results/ci")
    records = collections(directory, machine=machine)
    identifiers = [record["worker"] for record in records]
    (directory / "ui.xml").unlink(missing_ok=True)
    arguments = {worker: worker_arguments(directory, worker) for worker in identifiers}
    for worker in identifiers:
        prepare_worker_reports(directory, worker)

    def launch(worker):
        """Keep each process duration, exit status and original artifact location visible."""
        started = monotonic()
        result = subprocess.run([*compose, *arguments[worker]], check=False)
        print(
            f"Browser worker {worker}: {monotonic() - started:.2f}s, exit {result.returncode}. "
            f"Original reports: {worker_directory(directory, worker)}",
            flush=True,
        )
        return result

    with ThreadPoolExecutor(max_workers=BROWSER_WORKERS_PER_MACHINE) as workers:
        results = list(workers.map(launch, identifiers))
    for result, record in zip(results, records, strict=True):
        if result.returncode != 5 or record["selected"]:
            result.check_returncode()
    if machine is None:
        combine_reports(directory)
    else:
        execution_reports(directory, machine=machine)
    count = sum(len(record["selected"]) for record in records)
    print(
        f"Browser execution: {count} assigned identities executed exactly once "
        f"across workers {identifiers}.",
        flush=True,
    )
