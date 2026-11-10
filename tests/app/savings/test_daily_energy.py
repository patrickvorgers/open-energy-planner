"""Measured energy distinguishes missing channel associations from explicit recorded zeroes."""

from dataclasses import replace

import pytest

from open_energy_planner.savings.daily import add_energy
from tests.app.savings.support.scenarios import bindings, frame


@pytest.mark.parametrize(
    "binding, metric",
    [
        ("imports", "grid_import"),
        ("exports", "grid_export"),
        ("terms", "household"),
        ("solar", "solar_ac"),
    ],
)
def test_empty_metric_binding_is_unavailable_without_hiding_other_measurements(binding, metric):
    """Absent associations have no measured total or coverage. Other metrics retain their own evidence."""
    selected = bindings()
    selected[binding] = []
    totals = {
        name: {"kwh": None, "coverage_seconds": 0.0}
        for name in ("grid_import", "grid_export", "household", "solar_ac")
    }
    add_energy(totals, frame(), selected)
    assert totals[metric] == {"kwh": None, "coverage_seconds": 0.0}
    for name, value in totals.items():
        if name != metric:
            assert value["coverage_seconds"] == 900
            assert value["kwh"] == (1 if name in ("grid_import", "household") else 0)


def test_explicit_zero_channels_are_valid_measured_energy():
    """A complete associated zero reading remains zero with a full measured quarter of coverage."""
    measured = replace(frame(), channels={name: 0 for name in frame().channels})
    totals = {
        name: {"kwh": None, "coverage_seconds": 0.0}
        for name in ("grid_import", "grid_export", "household", "solar_ac")
    }
    add_energy(totals, measured, bindings())
    assert all(value == {"kwh": 0, "coverage_seconds": 900} for value in totals.values())
