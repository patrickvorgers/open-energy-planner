"""Record selected pytest identities, outcomes and cgroup counters without changing test behavior."""

import json
import os
from pathlib import Path

from pytest import hookimpl


def pytest_collection_finish(session):
    """Instrument only the primary workload reports, excluding nested regression subprocesses."""
    report = getattr(session.config.option, "xmlpath", None)
    if not report or session.config.rootpath != Path("/workspace"):
        return
    destination = Path(report).resolve().with_suffix(".probe.json")
    if not destination.is_relative_to(Path("/workspace/results/ci")):
        return
    session.resource_probe = {
        "destination": destination,
        "selected": sorted(item.nodeid for item in session.items),
        "outcomes": {},
    }
    for item in session.items:
        item.user_properties.append(("oep_benchmark_nodeid", item.nodeid))


@hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Retain each actual phase outcome and duration, including setup and cleanup failures."""
    outcome = yield
    record = getattr(item.session, "resource_probe", None)
    if record is None:
        return
    report = outcome.get_result()
    phases = record["outcomes"].setdefault(item.nodeid, {})
    if report.when in phases:
        raise RuntimeError("Duplicate resource measurement phase")
    phases[report.when] = {"outcome": report.outcome, "seconds": report.duration}


def pytest_sessionfinish(session, exitstatus):
    """Keep original counters and identity evidence even when the selected suite fails."""
    record = getattr(session, "resource_probe", None)
    if record is None:
        return
    resources = {}
    for name in (
        "cpu.max",
        "cpu.stat",
        "memory.max",
        "memory.events",
        "memory.stat",
        "memory.peak",
        "memory.swap.max",
        "memory.swap.peak",
    ):
        path = Path("/sys/fs/cgroup") / name
        resources[name] = path.read_text(encoding="utf-8") if path.exists() else None
    destination = record.pop("destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            {
                **record,
                "exit_code": int(exitstatus),
                "container": os.uname().nodename,
                "resources": resources,
            }
        ),
        encoding="utf-8",
    )
