"""Explicit current installation fixtures for independent app and tariff tests."""

from dataclasses import replace

from open_energy_planner.installation.model import BatteryState, InstallationProblem, PVUnit
from open_energy_planner.solar.calculation.model import SolarInverter


def installation_problem(
    intervals,
    battery=None,
    *,
    grid_import_limit_w=4000,
    grid_export_limit_w=4000,
    export_bonuses=(),
):
    """Declare ideal separate devices without adding converter limits to small tariff fixtures.

    PV values are independently supplied interval energies. The ideal converter
    preserves those values on both sides. Individual tests own physical limits
    and expected costs rather than deriving either from production constants.
    """
    solar = any(step.pv_kwh for step in intervals)
    return InstallationProblem(
        tuple(
            replace(step, pv_sources=(("pv", step.pv_kwh),) if solar else ()) for step in intervals
        ),
        (
            *((SolarInverter("pv-inverter", 10000000, 1, False),) if solar else ()),
            *((SolarInverter("battery-inverter", 10000000, 1, False),) if battery else ()),
        ),
        (BatteryState("battery", "battery-inverter", battery),) if battery else (),
        (PVUnit("pv", "pv-inverter", False),) if solar else (),
        grid_import_limit_w,
        grid_export_limit_w,
        export_bonuses=export_bonuses,
    )
