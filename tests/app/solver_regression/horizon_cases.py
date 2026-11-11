"""Original compatible 48-hour arithmetic on the current installation model."""

from dataclasses import dataclass, replace
from datetime import timedelta
from fractions import Fraction

import pytest

from open_energy_planner.installation.model import InstallationProblem
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.domain import Battery, Interval
from tests.app.solver_regression.cases import START, installation


def grid(profile):
    """Integrate power profiles into 96 quarter-hours followed by 24 hourly energy intervals."""
    result = []
    elapsed = Fraction(0)
    for duration in [Fraction(1, 4)] * 96 + [Fraction(1)] * 24:
        buy, sell, load_kw, pv_kw = profile(elapsed)
        result.append(
            Interval(
                START + timedelta(hours=float(elapsed)),
                float(duration),
                float(buy),
                float(sell),
                float(load_kw * duration),
                float(pv_kw * duration),
            )
        )
        elapsed += duration
    return tuple(result)


@dataclass(frozen=True)
class HorizonCase:
    """Preserve exact hourly balance and lossy-arbitrage bounds.

    H03 buys 10 kWh for 1, sells 8.1 kWh for 2.835 and pays 0.45 wear,
    giving -1.385 cash. H04/H05/H06 retirement is explained in the ledger.
    """

    name: str
    problem: InstallationProblem
    cash: Fraction
    imports: Fraction
    exports: Fraction
    final_energy: Fraction | None

    @property
    def objective(self):
        """These retained cases have no terminal continuation value."""
        return self.cash


def household(elapsed_hours):
    """Provide repeatable night/day/evening household demand and prices without PV."""
    hour = int(elapsed_hours % 24)
    load = (
        Fraction(35, 100) if hour < 6 else Fraction(8, 10) if 17 <= hour < 22 else Fraction(5, 10)
    )
    price = (
        Fraction(18, 100)
        if hour < 6
        else Fraction(34, 100)
        if 17 <= hour < 22
        else Fraction(25, 100)
    )
    return price, Fraction(6, 100), load, Fraction(0)


def pv_household(elapsed_hours):
    """Provide midday PV surplus with negative export prices for direct-flow arithmetic."""
    sunny = 10 <= elapsed_hours % 24 < 14
    return (
        Fraction(4, 100) if sunny else Fraction(25, 100),
        Fraction(-6, 100) if sunny else Fraction(6, 100),
        Fraction(5, 10),
        Fraction(4) if sunny else Fraction(0),
    )


def direct_totals(profile):
    """Independent exact hourly energy arithmetic. Profiles are constant inside hours."""
    purchased, exported, cost = Fraction(0), Fraction(0), Fraction(0)
    for hour in range(48):
        buy, sell, load, pv = profile(Fraction(hour))
        incoming, outgoing = max(Fraction(0), load - pv), max(Fraction(0), pv - load)
        purchased += incoming
        exported += outgoing
        cost += buy * incoming - sell * outgoing
    return purchased, exported, cost


g_import, g_export, g_cost = direct_totals(household)
p_import, p_export, p_cost = direct_totals(pv_household)

HORIZON_CASES = (
    HorizonCase("H01-grid-load", installation(grid(household)), g_cost, g_import, g_export, None),
    HorizonCase(
        "H02-fixed-pv-negative-export",
        installation(grid(pv_household)),
        p_cost,
        p_import,
        p_export,
        None,
    ),
    HorizonCase(
        "H03-lossy-arbitrage",
        installation(
            grid(
                lambda elapsed_hours: (
                    Fraction(1, 10) if elapsed_hours < 24 else Fraction(1, 2),
                    Fraction(0) if elapsed_hours < 24 else Fraction(35, 100),
                    Fraction(0),
                    Fraction(0),
                )
            ),
            battery=Battery(
                10,
                1,
                minimum_kwh=1,
                charge_limit_w=5000,
                discharge_limit_w=5000,
                charge_efficiency=0.9,
                discharge_efficiency=0.9,
                wear_cost_per_kwh=0.05,
            ),
            import_limit=6000,
            export_limit=6000,
            battery_rating=6000,
        ),
        Fraction("-1.385"),
        Fraction(10),
        Fraction("8.1"),
        Fraction(1),
    ),
)


def check_horizon_plan(case, plan):
    """Compare actual physical totals against the original independent horizon arithmetic."""
    costs = validate(case.problem, plan)
    for name, actual, expected in (
        ("cash", costs.cash_cost, case.cash),
        ("terminal", costs.terminal_value, 0),
        ("objective", costs.objective, case.objective),
        ("imports", sum(flow.grid_import for flow in plan.flows), case.imports),
        ("exports", sum(flow.grid_export for flow in plan.flows), case.exports),
    ):
        assert abs(actual - float(expected)) <= 1e-6, (case.name, name, actual, expected)
    if case.final_energy is None:
        assert not plan.flows[-1].batteries
    else:
        assert plan.flows[-1].batteries[0].end_kwh == pytest.approx(
            float(case.final_energy), abs=1e-6
        )
    return costs


def quarter_hour_problem(problem):
    """Refine constant hourly inputs and every source quantity without changing total energy."""
    steps = []
    for interval in problem.intervals:
        count = 4 if interval.duration_h == 1 else 1
        for offset in range(count):
            steps.append(
                replace(
                    interval,
                    start=interval.start + timedelta(minutes=15 * offset),
                    duration_h=0.25,
                    load_kwh=interval.load_kwh / count,
                    pv_kwh=interval.pv_kwh / count,
                    pv_sources=tuple(
                        (source, energy / count) for source, energy in interval.pv_sources
                    ),
                )
            )
    return replace(problem, intervals=tuple(steps))
