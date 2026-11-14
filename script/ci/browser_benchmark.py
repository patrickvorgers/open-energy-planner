"""Compare browser resources without changing release CI or its worker evidence contracts."""

import argparse
import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic

from pytest import hookimpl

from script.ci.browser_suites import collections, execution_reports
from script.ci.check_browser import prepare_worker_reports
from script.ci.pytest_suites import pytest_arguments as suite_arguments


@hookimpl(tryfirst=True)
def pytest_collection_modifyitems(session, config, items):
    """Split machines before the unchanged two-worker plugin partitions each machine's cases."""
    count = int(os.environ["OEP_BENCH_MACHINES"])
    index = int(os.environ["OEP_BENCH_MACHINE"])
    if count not in (1, 2) or index not in range(count):
        raise ValueError("Invalid benchmark machine")
    identifiers = sorted(item.nodeid for item in items)
    selected = identifiers[index::count]
    retained = set(selected)
    config.hook.pytest_deselected(items=[item for item in items if item.nodeid not in retained])
    items[:] = [item for item in items if item.nodeid in retained]
    location = Path("results/ci")
    location.mkdir(parents=True, exist_ok=True)
    (location / "benchmark-inventory.json").write_text(
        json.dumps({"all": identifiers, "selected": selected}), encoding="utf-8"
    )
    session.benchmark_phases = {}


@hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Measure fixture setup, interaction and cleanup separately without changing their outcomes."""
    outcome = yield
    report = outcome.get_result()
    item.session.benchmark_phases.setdefault(item.nodeid, {})[report.when] = report.duration


def pytest_sessionfinish(session, exitstatus):
    """Preserve cgroup pressure counters, distinguishing memory cache from reclaim and OOM events."""
    if not hasattr(session, "benchmark_phases"):
        return
    resources = {}
    for name in ("memory.events", "memory.stat", "memory.peak", "cpu.stat", "memory.swap.peak"):
        path = Path("/sys/fs/cgroup") / name
        resources[name] = path.read_text(encoding="utf-8") if path.exists() else None
    (Path("results/ci") / "benchmark-resources.json").write_text(
        json.dumps({"resources": resources, "phase_seconds": session.benchmark_phases}),
        encoding="utf-8",
    )


def run(image, variant, machine, directory):
    """Run the assigned selection with fixed experimental CPU and memory limits."""
    machines = 2 if variant in {"split", "split-wide"} else 1
    if machine not in range(machines):
        raise ValueError("Invalid benchmark machine")
    memory = (
        "4g"
        if variant in {"wide", "split-wide"}
        else ("2g" if variant in {"memory", "cpu"} else "1g")
    )
    cpus = "2" if variant in {"cpu", "wide", "split-wide"} else "1"
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    identity = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]["Id"]
    started = monotonic()

    def launch(worker):
        """Keep collection, execution and telemetry private to this worker."""
        location = directory / f"browser-{worker}"
        location.mkdir(parents=True, exist_ok=True)
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cpus",
            cpus,
            "--memory",
            memory,
            "--shm-size",
            "256m",
            "--volume",
            f"{location}:/workspace/results/ci",
            "--env",
            f"OEP_BENCH_MACHINES={machines}",
            "--env",
            f"OEP_BENCH_MACHINE={machine}",
            image,
        ]
        arguments = ("-p", "script.ci.browser_benchmark")
        prepare_worker_reports(directory, worker, collect_only=True)
        subprocess.run(
            [
                *command,
                *suite_arguments("browser", worker=worker, collect_only=True, arguments=arguments),
            ],
            check=True,
        )
        prepare_worker_reports(directory, worker)
        subprocess.run(
            [*command, *suite_arguments("browser", worker=worker, arguments=arguments)],
            check=True,
        )

    successful = False
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(launch, range(2)))
        execution_reports(directory)
        successful = True
    finally:
        (directory / "benchmark.json").write_text(
            json.dumps(
                {
                    "variant": variant,
                    "machine": machine,
                    "image_id": identity,
                    "seconds": monotonic() - started,
                    "memory_per_worker": memory,
                    "cpus_per_worker": cpus,
                    "successful": successful,
                    "host_cpus": os.cpu_count(),
                    "host_memory": Path("/proc/meminfo").read_text(encoding="utf-8"),
                }
            ),
            encoding="utf-8",
        )


def summarize(directory, *, comparison="initial"):
    """Reject omitted, duplicated or unsuccessful cases before comparing full-suite elapsed times."""
    variants = {}
    full_inventory = None
    image_id = None
    groups = {
        "initial": (("baseline", 1), ("memory", 1), ("split", 2)),
        "cpu": (("memory", 1), ("cpu", 1)),
        "wide": (("wide", 1), ("split-wide", 2)),
    }[comparison]
    for variant, machines in groups:
        elapsed = []
        executed = []
        for machine in range(machines):
            location = Path(directory) / f"{variant}-{machine}"
            metadata = json.loads((location / "benchmark.json").read_text(encoding="utf-8"))
            if metadata["variant"] != variant or metadata["machine"] != machine:
                raise ValueError("Benchmark identity differs")
            image_id = image_id or metadata["image_id"]
            if metadata["image_id"] != image_id:
                raise ValueError("Benchmark images differ")
            execution_reports(location)
            records = collections(location)
            for worker in range(2):
                inventory = json.loads(
                    (location / f"browser-{worker}" / "benchmark-inventory.json").read_text(
                        encoding="utf-8"
                    )
                )
                full_inventory = full_inventory or inventory["all"]
                if (
                    inventory["all"] != full_inventory
                    or inventory["selected"] != full_inventory[machine::machines]
                    or records[worker]["all"] != inventory["selected"]
                ):
                    raise ValueError("Benchmark inventories differ")
                executed.extend(records[worker]["selected"])
            elapsed.append(metadata["seconds"])
        if sorted(executed) != full_inventory:
            raise ValueError("Benchmark did not execute every case exactly once")
        variants[variant] = {
            "seconds": max(elapsed),
            "machine_seconds": sum(elapsed),
            "cases": len(executed),
        }
    result = {"image_id": image_id, "variants": variants}
    inventory_digest = hashlib.sha256(
        json.dumps(full_inventory, separators=(",", ":")).encode()
    ).hexdigest()
    expected = os.environ.get("OEP_BENCH_EXPECTED_INVENTORY")
    if expected and expected != inventory_digest:
        raise ValueError("Benchmark differs from the independently proven source inventory")
    result["inventory_sha256"] = inventory_digest
    (Path(directory) / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


def main():
    """Expose only the fixed experimental variants and the final evidence comparison."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "summarize", "compare-cpu", "compare-wide"))
    parser.add_argument("--image", default="oep-browser-benchmark")
    parser.add_argument(
        "--variant", choices=("baseline", "memory", "split", "cpu", "wide", "split-wide")
    )
    parser.add_argument("--machine", type=int, default=0)
    parser.add_argument("--directory", required=True)
    options = parser.parse_args()
    if options.action == "run":
        if options.variant is None:
            parser.error("run requires --variant")
        run(options.image, options.variant, options.machine, options.directory)
    else:
        comparison = {"summarize": "initial", "compare-cpu": "cpu", "compare-wide": "wide"}
        summarize(options.directory, comparison=comparison[options.action])


if __name__ == "__main__":
    main()
