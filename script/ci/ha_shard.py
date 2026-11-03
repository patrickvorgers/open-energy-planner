"""Partition real HA tests across configured workers without sharing HA state or Recorder stores."""

from pytest import hookimpl

from script.ci.ha_suites import HA_WORKERS

RECORDER_HEAVY_TESTS = (
    "test_native_test_data_reuses_energy_ids_units_and_preferences",
    "test_fresh_lab_seeds_household_before_individual_consumer",
    "test_seeded_lab_without_schema_marker_preserves_energy_configuration",
)


def pytest_addoption(parser):
    """Require an explicit worker index whenever the HA partition plugin is loaded."""
    parser.addoption("--ha-shard", type=int, choices=range(HA_WORKERS), required=True)


@hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Keep every collected case in exactly one process and serialize tests sharing the external app.

    Recorder-heavy cases start first and alternate between workers. All remote
    app tests stay in worker zero. Each process owns its other temporary HA,
    Recorder, ports and module state. No independent expectation is changed.
    """
    worker = config.getoption("ha_shard")
    ordered = sorted(
        items,
        key=lambda item: (
            not any(name in item.nodeid for name in RECORDER_HEAVY_TESTS),
            item.nodeid,
        ),
    )
    assignments = {}
    independent_index = 0
    for item in ordered:
        if "::test_remote_" in item.nodeid:
            assignments[item.nodeid] = 0
        else:
            assignments[item.nodeid] = independent_index % HA_WORKERS
            independent_index += 1
    selected = [item for item in ordered if assignments[item.nodeid] == worker]
    deselected = [item for item in items if assignments[item.nodeid] != worker]
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
