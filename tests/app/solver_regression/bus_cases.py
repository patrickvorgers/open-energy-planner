"""Original compatible shared-meter expectations on explicit installation devices."""

from dataclasses import dataclass, replace
from datetime import timedelta
from fractions import Fraction

from open_energy_planner.installation.model import BatteryState, InstallationProblem, PVUnit
from open_energy_planner.optimization.domain import Battery, Interval
from open_energy_planner.solar.calculation.model import SolarInverter
from tests.app.solver_regression.cases import START, valuation


def slots(buy, sell, *, loads=None, pv=None):
    """Preserve original quarter-hour price, demand and per-source PV energy inputs."""
    return tuple(
        Interval(
            START + timedelta(minutes=15 * index),
            0.25,
            purchase,
            sell[index],
            loads[index] if loads else 0,
            pv[index] if pv else 0,
        )
        for index, purchase in enumerate(buy)
    )


def storage(device_id, initial=0, *, charge=4000, discharge=4000, **settings):
    """Declare independent 1-kWh current battery state and terminal ratings."""
    return BatteryState(
        device_id,
        device_id,
        Battery(1, initial, charge_limit_w=charge, discharge_limit_w=discharge, **settings),
    )


def network(
    steps, units, *, inverters=None, rates=(), import_limit=4000, export_limit=4000, pv_rating=4000
):
    """Declare current physical converter ports rather than old source-to-sink routes."""
    steps = tuple(replace(step, pv_sources=(("pv", step.pv_kwh),)) for step in steps)
    converters = tuple(SolarInverter(unit.inverter_id, 4000, 1) for unit in units)
    if inverters is not None:
        converters = inverters
    converters += (SolarInverter("pv-inverter", pv_rating, 1),)
    return InstallationProblem(
        steps,
        converters,
        units,
        (PVUnit("pv", "pv-inverter", False),),
        import_limit,
        export_limit,
        terminal_valuations=tuple(
            valuation(unit, rate, steps) for unit, rate in zip(units, rates, strict=True)
        )
        if rates
        else (),
    )


@dataclass(frozen=True)
class BusCase:
    """Keep original shared-grid costs, total exchanges and named final stocks.

    M01 imports 1 including .25 load and later sells .75: -.275 cash.
    M02 sells .75 and keeps .5 worth .1. M03 later sells the original 1.
    M04's old asymmetric .72 total is retired. M05 avoids 1.2 wear,
    sells .5 and keeps .5 worth .05. M06 stores two .5 portions worth .25.
    M08 chooses .5 export income over .2 import income.
    """

    name: str
    problem: InstallationProblem
    cash: Fraction
    terminal: Fraction
    imports: Fraction
    exports: Fraction
    final: tuple

    @property
    def objective(self):
        """Subtract independently valued stock from the specified cash cost."""
        return self.cash - self.terminal


M01 = network(
    slots((0.1, 0.8), (0, 0.5), loads=(0.25, 0)),
    (storage("A", charge=2000), storage("B", charge=2000)),
)
M02 = network(
    slots((0.8,), (0.5,)),
    (storage("A", 1), storage("B", 0.25)),
    inverters=(SolarInverter("A", 2000, 1), SolarInverter("B", 4000, 1)),
    rates=(0.2, 0.2),
    export_limit=3000,
)
M03 = network(
    slots((1, 1), (0, 1)),
    (storage("A", 1, discharge=2000), storage("B")),
    import_limit=0,
    export_limit=8000,
)
M04 = replace(
    M03,
    inverters=(
        SolarInverter("A", 4000, 0.8),
        SolarInverter("B", 4000, 0.8),
        SolarInverter("pv-inverter", 4000, 1),
    ),
)
M05 = network(
    M03.intervals,
    (storage("A", 1, discharge=2000), storage("B", cycle_fee=1.2)),
    rates=(0.1, 0),
    import_limit=0,
    export_limit=8000,
)
M06 = network(
    slots((1,), (0,), loads=(0.5,), pv=(1.5,)),
    (storage("A", charge=2000), storage("B", charge=2000)),
    rates=(0.3, 0.2),
    import_limit=0,
    export_limit=0,
    pv_rating=6000,
)
M07 = network(slots((0.2,), (0.1,), loads=(1.1,)), (storage("A"), storage("B")))
M08 = network(slots((-0.2,), (0.5,)), (storage("A"), storage("B", 1)))
EMPTY = (("A", Fraction(0)), ("B", Fraction(0)))
BUS_CASES = (
    BusCase(
        "M01-shared-import",
        M01,
        Fraction("-.275"),
        Fraction(0),
        Fraction(1),
        Fraction(".75"),
        EMPTY,
    ),
    BusCase(
        "M02-shared-export",
        M02,
        Fraction("-.375"),
        Fraction(".1"),
        Fraction(0),
        Fraction(".75"),
        (("A", Fraction(".5")), ("B", Fraction(0))),
    ),
    BusCase(
        "M03-beneficial-transfer", M03, Fraction(-1), Fraction(0), Fraction(0), Fraction(1), EMPTY
    ),
    BusCase(
        "M05-transfer-wear",
        M05,
        Fraction("-.5"),
        Fraction(".05"),
        Fraction(0),
        Fraction(".5"),
        (("A", Fraction(".5")), ("B", Fraction(0))),
    ),
    BusCase(
        "M06-shared-pv",
        M06,
        Fraction(0),
        Fraction(".25"),
        Fraction(0),
        Fraction(0),
        (("A", Fraction(".5")), ("B", Fraction(".5"))),
    ),
    BusCase(
        "M08-shared-direction", M08, Fraction("-.5"), Fraction(0), Fraction(0), Fraction(1), EMPTY
    ),
)


def horizon(problem):
    """Keep the first two quarter opportunities and append an explicitly empty 48-hour tail."""
    steps, elapsed = [], 0.0
    for index, duration in enumerate([0.25] * 96 + [1] * 24):
        step = (
            problem.intervals[index]
            if index < len(problem.intervals)
            else Interval(
                START + timedelta(hours=elapsed),
                duration,
                1,
                0,
                pv_sources=tuple((source.source_id, 0) for source in problem.pv_sources),
            )
        )
        steps.append(replace(step, duration_h=duration))
        elapsed += duration
    return replace(problem, intervals=tuple(steps))
