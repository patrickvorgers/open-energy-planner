"""Check battery flow attribution independently of the solver and browser."""

import pytest

from open_energy_planner.installation.battery_flows import battery_flow_attribution


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
        "grid_import": 1.3,
        "grid_export": 0,
        "batteries": [
            {"device_id": "home", "charge_kwh": 1.08, "discharge_kwh": 0},
            {"device_id": "garage", "charge_kwh": 1.2, "discharge_kwh": 0},
        ],
        "solar": [{"source_id": "roof", "used_kwh": 0.6}, {"source_id": "shed", "used_kwh": 1.25}],
        "inverters": [
            {"inverter_id": "hybrid", "import_kwh": 2, "export_kwh": 0},
            {"inverter_id": "solar", "import_kwh": 0, "export_kwh": 1},
        ],
    }
    result, missing = battery_flow_attribution(problem, [flow, {**flow, "solar": []}])
    # Both batteries draw 1.2 kWh on the shared DC side. The house gets 0.3
    # of the 1 kWh PV AC and the batteries share the remaining 0.7.
    assert result["home"]["charge"] == pytest.approx(
        {"pv_dc": 0.27, "pv_ac": 0.2835, "grid_import": 0.5265, "other": 0}
    )
    assert result["garage"]["charge"] == pytest.approx(
        {"pv_dc": 0.3, "pv_ac": 0.315, "grid_import": 0.585, "other": 0}
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
        "grid_export": 0,
        "batteries": [
            {"device_id": "home", "charge_kwh": 0.4, "discharge_kwh": 0},
            {"device_id": "garage", "charge_kwh": 1.35, "discharge_kwh": 0},
        ],
        "solar": [{"source_id": "roof", "used_kwh": 1.4}],
        "inverters": [
            {"inverter_id": "hybrid", "import_kwh": 0, "export_kwh": 0.8},
            {"inverter_id": "storage", "import_kwh": 1.5, "export_kwh": 0},
        ],
    }
    [result] = battery_flow_attribution(problem, [flow])
    assert result["home"]["charge"] == pytest.approx(
        {"pv_dc": 0.4, "pv_ac": 0, "grid_import": 0, "other": 0}
    )
    assert result["garage"]["charge"] == pytest.approx(
        {"pv_dc": 0, "pv_ac": 0.72, "grid_import": 0.63, "other": 0}
    )


def test_battery_discharge_routes_follow_pv_and_preserve_terminal_totals():
    """PV supplies 0.8 kWh of load before batteries share the remaining load and export."""
    problem = {
        "inverters": [
            {"inverter_id": "hybrid", "efficiency": 0.9},
            {"inverter_id": "solar", "efficiency": 0.8},
        ],
        "batteries": [
            {"device_id": "home", "inverter_id": "hybrid", "dc_efficiency": 0.9},
            {"device_id": "garage", "inverter_id": "hybrid", "dc_efficiency": 1},
        ],
        "pv_sources": [{"source_id": "roof", "inverter_id": "solar"}],
    }
    flow = {
        "grid_import": 0,
        "grid_export": 0.936,
        "batteries": [
            {"device_id": "home", "charge_kwh": 0, "discharge_kwh": 1.2},
            {"device_id": "garage", "charge_kwh": 0, "discharge_kwh": 1},
        ],
        "solar": [{"source_id": "roof", "used_kwh": 1}],
        "inverters": [
            {"inverter_id": "hybrid", "import_kwh": 0, "export_kwh": 1.872},
            {"inverter_id": "solar", "import_kwh": 0, "export_kwh": 0.8},
        ],
    }
    [result] = battery_flow_attribution(problem, [flow])
    assert result["home"]["discharge"] == pytest.approx(
        {"own_use": 0.6, "grid_export": 0.6, "other": 0}
    )
    assert result["garage"]["discharge"] == pytest.approx(
        {"own_use": 0.5, "grid_export": 0.5, "other": 0}
    )


def test_battery_transfer_keeps_unattributed_energy_in_both_directions():
    """A transfer between batteries produces no on-site use, solar or grid flow."""
    problem = {
        "inverters": [{"inverter_id": "hybrid", "efficiency": 0.9}],
        "pv_sources": [],
        "batteries": [
            {"device_id": "home", "inverter_id": "hybrid", "dc_efficiency": 0.9},
            {"device_id": "garage", "inverter_id": "hybrid", "dc_efficiency": 1},
        ],
    }
    flow = {
        "grid_import": 0,
        "grid_export": 0,
        "batteries": [
            {"device_id": "home", "charge_kwh": 0, "discharge_kwh": 1},
            {"device_id": "garage", "charge_kwh": 0.9, "discharge_kwh": 0},
        ],
        "solar": [],
        "inverters": [{"inverter_id": "hybrid", "import_kwh": 0, "export_kwh": 0}],
    }
    [result] = battery_flow_attribution(problem, [flow])
    assert result["home"]["discharge"] == {"own_use": 0, "grid_export": 0, "other": 1}
    assert result["garage"]["charge"] == {"pv_dc": 0, "pv_ac": 0, "grid_import": 0, "other": 0.9}


def test_attribution_accepts_the_plan_validators_energy_roundoff():
    """A validated near-zero grid flow is clamped and a larger negative flow is unavailable."""
    problem = {
        "inverters": [{"inverter_id": "hybrid", "efficiency": 1}],
        "pv_sources": [{"source_id": "roof", "inverter_id": "hybrid"}],
        "batteries": [{"device_id": "home", "inverter_id": "hybrid", "dc_efficiency": 1}],
    }
    flow = {
        "grid_import": -9e-8,
        "grid_export": 0,
        "batteries": [{"device_id": "home", "charge_kwh": 1, "discharge_kwh": 0}],
        "solar": [{"source_id": "roof", "used_kwh": 1}],
        "inverters": [{"inverter_id": "hybrid", "import_kwh": 0, "export_kwh": 0}],
    }
    rounded, unavailable = battery_flow_attribution(
        problem, [flow, {**flow, "grid_import": -1.1e-7}]
    )
    assert rounded["home"]["charge"] == {"pv_dc": 1, "pv_ac": 0, "grid_import": 0, "other": 0}
    assert unavailable == {}
