"""Hand-derived core expectations expressed through current installation contracts."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction

from open_energy_planner.installation.model import BatteryState, InstallationProblem, PVUnit
from open_energy_planner.installation.valuation import TerminalValuation
from open_energy_planner.optimization.domain import Battery, Interval
from open_energy_planner.solar.calculation.model import SolarInverter

START = datetime(2026, 2, 3, tzinfo=UTC)


def intervals(buy, sell, *, pv=0, load=0):
    """Retain the original contiguous quarter-hour price and energy inputs."""
    return tuple(
        Interval(START + timedelta(minutes=15 * index), 0.25, purchase, sale, load, pv)
        for index, (purchase, sale) in enumerate(zip(buy, sell, strict=True))
    )


def valuation(unit, rate, steps):
    """Declare an explicit bounded continuation for that battery's usable stock only."""
    end = steps[-1].start + timedelta(hours=steps[-1].duration_h)
    return TerminalValuation(
        unit.device_id,
        unit.battery.minimum_kwh,
        unit.battery.capacity_kwh - unit.battery.minimum_kwh,
        rate,
        end,
        end + timedelta(hours=1),
        "post_horizon_prices",
    )


def installation(
    steps,
    *,
    battery=None,
    terminal_rate=0,
    hybrid=False,
    pv_efficiency=1,
    battery_efficiency=1,
    dc_efficiency=1,
    pv_rating=4000,
    battery_rating=4000,
    can_curtail=False,
    import_limit=4000,
    export_limit=4000,
):
    """Build actual per-device DC/AC contracts, without an old-model conversion layer."""
    steps = tuple(replace(step, pv_sources=(("pv", step.pv_kwh),)) for step in steps)
    inverters = (SolarInverter("pv-inverter", pv_rating, pv_efficiency, hybrid),)
    units = ()
    if battery is not None:
        if not hybrid:
            inverters += (SolarInverter("battery-inverter", battery_rating, battery_efficiency),)
        units = (
            BatteryState(
                "battery", "pv-inverter" if hybrid else "battery-inverter", battery, dc_efficiency
            ),
        )
    return InstallationProblem(
        steps,
        inverters,
        units,
        (PVUnit("pv", "pv-inverter", can_curtail),),
        import_limit,
        export_limit,
        terminal_valuations=tuple(valuation(unit, terminal_rate, steps) for unit in units)
        if terminal_rate
        else (),
    )


@dataclass(frozen=True)
class Case:
    """Retain the original S01 spread, S02 paid charging and S04 usable-stock arithmetic.

    S01 buys 1 kWh at 0.12 and sells it at 0.40, giving -0.28 cash cost.
    S02 is paid 0.16 for charging 1 kWh. S04 retains 1 kWh worth 0.28.
    S03 used obsolete continuous PV curtailment and direct-PV-only bonus credit.
    Its replacement evidence is recorded in SOLVER-REGRESSION-MIGRATION.md.
    """

    name: str
    problem: InstallationProblem
    cash: Fraction
    terminal: Fraction
    objective: Fraction
    imports: tuple
    exports: tuple
    energy: tuple
    battery_power_w: tuple


CORE = (
    Case(
        "S01",
        installation(intervals((0.12, 0.50), (0.04, 0.40)), battery=Battery(1, 0)),
        Fraction("-0.28"),
        Fraction(0),
        Fraction("-0.28"),
        (1, 0),
        (0, 1),
        (1, 0),
        (-4000, 4000),
    ),
    Case(
        "S02",
        installation(intervals((-0.16,), (-0.24,)), battery=Battery(1, 0)),
        Fraction("-0.16"),
        Fraction(0),
        Fraction("-0.16"),
        (1,),
        (0,),
        (1,),
        (-4000,),
    ),
    Case(
        "S04",
        installation(
            intervals((0.30, 0.30), (0.10, 0.14)), battery=Battery(1, 1), terminal_rate=0.28
        ),
        Fraction(0),
        Fraction("0.28"),
        Fraction("-0.28"),
        (0, 0),
        (0, 0),
        (1, 1),
        (0, 0),
    ),
)
