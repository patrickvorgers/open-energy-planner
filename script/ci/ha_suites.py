"""Own HA test directories, isolated worker count and shard arguments without process execution."""

from script.ci.ci_groups import HA_TEST_DIRECTORIES, PYTEST_AREAS

HA_WORKERS = 2


def ha_selection(suite, worker=None):
    """Return HA-only directories/plugin arguments and the worker's independent report name."""
    if suite != "ha":
        if not suite.startswith("ha-"):
            raise ValueError("Expected an HA suite")
        return list(PYTEST_AREAS[suite]), suite
    if worker is None:
        raise ValueError("HA execution and collection require a worker")
    return [
        *HA_TEST_DIRECTORIES,
        "-p",
        "script.ci.ha_shard",
        "--ha-shard",
        str(worker),
    ], f"ha-{worker}"
