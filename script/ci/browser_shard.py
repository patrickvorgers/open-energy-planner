"""Record exact browser collection and per-phase execution in isolated pytest workers."""

import json
from pathlib import Path

from pytest import hookimpl

from script.ci.browser_suites import BROWSER_WORKERS, NODE_PROPERTY, partition


def pytest_addoption(parser):
    """Require an explicit worker whenever the browser partition plugin is active."""
    parser.addoption("--browser-shard", type=int, choices=range(BROWSER_WORKERS), required=True)


@hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    """Partition after ordinary pytest filters and attach stable identities to actual JUnit cases."""
    worker = config.getoption("browser_shard")
    identifiers = sorted(item.nodeid for item in items)
    selected = partition(identifiers, worker)
    selected_set = set(selected)
    session.browser_record = {"worker": worker, "all": identifiers, "selected": selected}
    session.browser_outcomes = {}
    retained = {item.nodeid: item for item in items}
    deselected = [item for item in items if item.nodeid not in selected_set]
    config.hook.pytest_deselected(items=deselected)
    items[:] = [retained[identity] for identity in selected]
    for item in items:
        item.user_properties.append((NODE_PROPERTY, item.nodeid))
    location = Path("results/ci")
    location.mkdir(parents=True, exist_ok=True)
    if config.option.collectonly:
        (location / "browser-collected.json").write_text(
            json.dumps(session.browser_record), encoding="utf-8"
        )


@hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Retain actual setup, call and teardown outcomes rather than trusting an exit code alone."""
    outcome = yield
    report = outcome.get_result()
    phases = item.session.browser_outcomes.setdefault(item.nodeid, {})
    if report.when in phases:
        raise RuntimeError("Duplicate browser execution phase")
    phases[report.when] = report.outcome


def pytest_sessionfinish(session, exitstatus):
    """Write execution evidence only for an actual run with a completed collection."""
    if session.config.option.collectonly or not hasattr(session, "browser_record"):
        return
    try:
        peak_memory = int(Path("/sys/fs/cgroup/memory.peak").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        peak_memory = None
    (Path("results/ci") / "browser-executed.json").write_text(
        json.dumps(
            {
                **session.browser_record,
                "outcomes": session.browser_outcomes,
                "exit_code": int(exitstatus),
                "peak_memory_bytes": peak_memory,
            }
        ),
        encoding="utf-8",
    )
