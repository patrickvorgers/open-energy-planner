"""Bound diagnostic configuration properties before they become Home Assistant attributes."""

import pytest

from custom_components.open_energy_planner.protocol import validate_device_configuration


@pytest.mark.parametrize(
    "device",
    [
        {"configured": "yes"},
        {"configuration": {"recover_reserve": "yes"}},
        {"configuration": {"calibration_enabled": "yes"}},
        {"configuration": {"calibration_interval_days": 0}},
        {"configuration": {"calibration_interval_days": 181}},
        {"configuration": {"calibration_interval_days": True}},
        {"configuration": {"calibration_hold_hours": float("nan")}},
        {"configuration": {"calibration_hold_hours": 0}},
        {"kind": "battery", "reserve_status": "charging"},
        {"kind": "solar", "reserve_status": "below_reserve"},
        {"configuration": {"unexpected": "private"}},
        {"configuration": {"capacity_kwh": float("inf")}},
        {"configuration": {"capacity_kwh": [10]}},
        {"configuration": {"soc_entity": "x" * 161}},
        {"configuration": {"inverter": {"ac_limit_w": 1000}}},
    ],
)
def test_configuration_rejects_unbounded_or_unknown_properties(device):
    """Malformed metadata cannot publish arbitrary attributes, nonfinite values or malformed inverter records."""
    with pytest.raises(ValueError):
        validate_device_configuration(device)


@pytest.mark.parametrize("interval_days", [1, 14, 180])
def test_configuration_preserves_units_permissions_and_explicit_optional_bindings(interval_days):
    """Small typed settings, including absent export and zero wear cost, remain valid without any active command."""
    validate_device_configuration(
        {
            "configured": True,
            "can_curtail": False,
            "energy_entity": "sensor.roof",
            "configuration": {
                "capacity_w": 3000,
                "cycle_fee": 0,
                "calibration_enabled": True,
                "calibration_interval_days": interval_days,
                "calibration_hold_hours": 3,
                "grid_export_entity": None,
                "inverter": {
                    "inverter_id": "hybrid",
                    "ac_limit_w": 3000,
                    "efficiency": 0.96,
                    "hybrid": True,
                },
            },
        }
    )
