"""Check charging source attribution independently of the solver and browser."""

import pytest

from open_energy_planner.installation.charge_sources import battery_charge_sources


def test_local_dc_is_used_before_ac_pv_and_grid_for_a_shared_converter():
    """Split 0.6 kWh local DC and 1.8 kWh converted AC across equal DC demands."""
    problem = {
        "inverters": [
            {"inverter_id": "hybrid", "efficiency": 0.9},
            {"inverter_id": "solar", "efficiency": 0.8},
        ],
        "batteries": [
            {"device_id": "home", "inverter_id": "hybrid", "dc_efficiency": 0.9},
            {"device_id": "garage", "inverter_id": "hybrid", "dc_efficiency": 1},
        ],
        "pv_sources": [
            {"source_id": "roof", "inverter_id": "hybrid"},
            {"source_id": "shed", "inverter_id": "solar"},
        ],
    }
    flow = {
        "grid_import": 1,
        "batteries": [
            {"device_id": "home", "charge_kwh": 1.08},
            {"device_id": "garage", "charge_kwh": 1.2},
        ],
        "solar": [{"source_id": "roof", "used_kwh": 0.6}, {"source_id": "shed", "used_kwh": 1.25}],
        "inverters": [
            {"inverter_id": "hybrid", "import_kwh": 2, "export_kwh": 0},
            {"inverter_id": "solar", "import_kwh": 0, "export_kwh": 1},
        ],
    }
    result, missing = battery_charge_sources(problem, [flow, {**flow, "solar": []}])
    # Both batteries draw 1.2 kWh on the shared DC side. Half of each AC kWh
    # comes from PV and half from the grid, with 90% inverter efficiency.
    assert result["home"] == pytest.approx(
        {"pv_dc": 0.27, "pv_ac": 0.405, "grid": 0.405, "other": 0}
    )
    assert result["garage"] == pytest.approx(
        {"pv_dc": 0.3, "pv_ac": 0.45, "grid": 0.45, "other": 0}
    )
    assert missing == {}


def test_exported_pv_is_bounded_after_direct_charging():
    """PV charges a local battery before 0.8 kWh becomes AC for the second battery."""
    problem = {
        "inverters": [
            {"inverter_id": "hybrid", "efficiency": 0.8},
            {"inverter_id": "storage", "efficiency": 0.9},
        ],
        "batteries": [
            {"device_id": "home", "inverter_id": "hybrid", "dc_efficiency": 1},
            {"device_id": "garage", "inverter_id": "storage", "dc_efficiency": 1},
        ],
        "pv_sources": [{"source_id": "roof", "inverter_id": "hybrid"}],
    }
    flow = {
        "grid_import": 0.7,
        "batteries": [
            {"device_id": "home", "charge_kwh": 0.4},
            {"device_id": "garage", "charge_kwh": 1.35},
        ],
        "solar": [{"source_id": "roof", "used_kwh": 1.4}],
        "inverters": [
            {"inverter_id": "hybrid", "import_kwh": 0, "export_kwh": 0.8},
            {"inverter_id": "storage", "import_kwh": 1.5, "export_kwh": 0},
        ],
    }
    [result] = battery_charge_sources(problem, [flow])
    assert result["home"] == pytest.approx({"pv_dc": 0.4, "pv_ac": 0, "grid": 0, "other": 0})
    assert result["garage"] == pytest.approx({"pv_dc": 0, "pv_ac": 0.72, "grid": 0.63, "other": 0})
