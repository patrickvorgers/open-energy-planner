"""Measured energy distinguishes missing channel associations from explicit recorded zeroes."""

from copy import deepcopy
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


@pytest.mark.parametrize(
    "binding, value",
    [
        ("imports", "sensor.import"),
        ("imports", ["sensor.import", "sensor.import"]),
        ("exports", [None]),
        ("terms", [{"entity_id": "sensor.import", "sign": True}]),
        ("terms", [{"entity_id": "sensor.import", "sign": 2}]),
        ("terms", [{"entity_id": "sensor.import"}]),
        ("terms", [{"entity_id": "sensor.import", "sign": 1}] * 2),
        ("solar", [None]),
        ("solar", [{"entity_id": "PRIVATE-BINDING-SENTINEL"}]),
    ],
)
def test_malformed_binding_cannot_partially_mutate_measured_totals(binding, value):
    """Validate every consumed association before any metric receives energy or coverage."""
    selected = bindings()
    selected[binding] = value
    totals = {
        name: {"kwh": 3, "coverage_seconds": 1800}
        for name in ("grid_import", "grid_export", "household", "solar_ac")
    }
    original = deepcopy(totals)
    with pytest.raises((ValueError, KeyError, TypeError)):
        add_energy(totals, frame(), selected)
    assert totals == original


def test_missing_channel_keeps_only_independently_measured_metrics():
    """A missing PV meter withdraws solar and household evidence without losing the grid readings."""
    channels = dict(frame().channels)
    del channels["sensor.pv"]
    totals = {
        name: {"kwh": None, "coverage_seconds": 0.0}
        for name in ("grid_import", "grid_export", "household", "solar_ac")
    }
    add_energy(totals, replace(frame(), channels=channels), bindings())
    assert totals == {
        "grid_import": {"kwh": 1, "coverage_seconds": 900},
        "grid_export": {"kwh": 0, "coverage_seconds": 900},
        "household": {"kwh": None, "coverage_seconds": 0},
        "solar_ac": {"kwh": None, "coverage_seconds": 0},
    }


def test_signed_household_balance_retains_kwh_without_changing_channel_directions():
    """2 import plus .75 solar plus .25 discharge minus .5 export yields 2.5 household kWh."""
    totals = {
        name: {"kwh": None, "coverage_seconds": 0.0}
        for name in ("grid_import", "grid_export", "household", "solar_ac")
    }
    add_energy(
        totals,
        frame(imported_kwh=2, exported_kwh=0.5, pv_kwh=0.75, discharged_kwh=0.25),
        bindings(),
    )
    assert totals == {
        "grid_import": {"kwh": 2, "coverage_seconds": 900},
        "grid_export": {"kwh": 0.5, "coverage_seconds": 900},
        "household": {"kwh": 2.5, "coverage_seconds": 900},
        "solar_ac": {"kwh": 0.75, "coverage_seconds": 900},
    }
