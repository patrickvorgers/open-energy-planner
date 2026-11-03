"""Hand-derived aggregate-export settlements and exhaustive small decision oracles."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from itertools import pairwise, product

import highspy
import pytest

from open_energy_planner.installation.model import (
    BatteryState,
    InstallationFlow,
    InstallationPlan,
    InstallationProblem,
    InverterFlow,
    PVFlow,
    PVUnit,
)
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.adapters.highs.installation import InstallationModelFactory
from open_energy_planner.optimization.domain import Battery, Costs, Interval, Status
from open_energy_planner.optimization.export_bonus import ExportBonus, ExportBonusResult, settle
from open_energy_planner.solar.calculation.model import SolarInverter

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


@pytest.fixture
def solver():
    """Solve only the current installation model, without the retired single-network builder."""
    return HighsSolver(factory=InstallationModelFactory())


def bonus(rates, caps, initial=0, periods=None, identity="extra"):
    """Prepare explicit per-segment rates/caps without a provider or rule compiler."""
    return ExportBonus(
        identity, tuple(rates), tuple(caps), tuple(periods or ["year"] * len(rates)), initial
    )


def network(exports, rules, prices=None, curtail=False):
    """Make PV export unavoidable by omitting demand/storage, unless curtailment is explicitly allowed."""
    steps = tuple(
        Interval(
            NOW + timedelta(minutes=15 * index),
            0.25,
            1,
            (prices or [0] * len(exports))[index],
            pv_kwh=value,
            pv_sources=(("pv", value),),
        )
        for index, value in enumerate(exports)
    )
    return InstallationProblem(
        steps,
        (SolarInverter("pv_inverter", 8000, 1),),
        (),
        (PVUnit("pv", "pv_inverter", curtail),),
        8000,
        8000,
        export_bonuses=tuple(rules),
    )


@pytest.mark.planner_scenario
@pytest.mark.parametrize(
    "exports,rates,caps,initial,periods,quota,counters,income",
    [
        ((2,), (0.03,), (10,), 0, None, (2,), (2,), ".06"),
        ((1, 1, 1), (0.05, 0, 0), (10, 10, 10), 0, None, (1, 1, 1), (1, 2, 3), ".05"),
        ((1,), (0.05,), (100,), 99.6, None, (0.4,), (100.6,), ".02"),
        ((1, 1), (0.02, 0.10), (1, 1), 0, None, (1, 0), (1, 2), ".02"),
        ((1, 1), (0, 0.05), (1, 1), 0, None, (1, 0), (1, 2), "0"),
        ((1, 1), (0.02, 0.06), (1.5, 1.5), 0, None, (1, 0.5), (1, 2), ".05"),
        ((1, 1), (0.05, 0.05), (1, 1), 1, ("old", "new"), (0, 1), (2, 1), ".05"),
        ((1, 1), (0.05, 0.05), (1, 3), 1, None, (0, 1), (2, 3), ".05"),
        ((1, 1), (0.05, 0.05), (3, 1), 1, None, (1, 0), (2, 3), ".05"),
        ((1,), (0.05,), (1,), 5, None, (0,), (6,), "0"),
    ],
)
def test_prescribed_exports_have_hand_calculated_settlement(
    solver, exports, rates, caps, initial, periods, quota, counters, income
):
    """Credit first volumes, not best-priced volumes. Zero-rate exports count and only a new period resets.

    Each row supplies literal quota/counter expectations and decimal income:
    e.g. 100-99.6=0.4 remaining gives 0.4*0.05=0.02. Cap increases must retain
    earlier uncredited exports. Cap decreases cannot grant additional quota.
    """
    rule = bonus(rates, caps, initial, periods)
    settled = settle(rule, exports)
    assert settled.quota_kwh == pytest.approx(quota)
    assert settled.counters_kwh == pytest.approx(counters)
    problem = network(exports, (rule,))
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL
    checked = validate(problem, result.plan)
    assert checked.bonus_income == pytest.approx(float(Fraction(income)), abs=1e-7)
    assert result.plan.export_bonus_results[0].quota_kwh == pytest.approx(quota)
    assert result.plan.export_bonus_results[0].counters_kwh == pytest.approx(counters)
    assert [flow.grid_export for flow in result.plan.flows] == pytest.approx(exports)


def test_base_income_is_separate_and_bonuses_are_additive(solver):
    """Two kWh earn 0.20 base plus capped A=0.02 and B=0.06, giving 0.28 total revenue."""
    problem = network(
        (2,), (bonus((0.02,), (1,), identity="A"), bonus((0.03,), (2,), identity="B")), (0.10,)
    )
    result = solver.solve(problem)
    costs = validate(problem, result.plan)
    assert costs.export_income == pytest.approx(0.20)
    assert costs.bonus_income == pytest.approx(0.08)
    assert costs.cash_cost == pytest.approx(-0.28)


@pytest.mark.planner_scenario
@pytest.mark.parametrize("cap", [6000, 7500])
def test_large_annual_cap_does_not_make_tiny_profitable_exports_infeasible(solver, cap):
    """Two 0.00001-kWh exports fit 0.4 quota and earn (0.05-0.01+0.10-0.02)*0.00001=0.0000012."""
    current = bonus((0.05, 0.10), (cap, cap), cap - 0.4)
    problem = network((0.00001, 0.00001), (current,), (-0.01, -0.02), curtail=True)
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL
    checked = validate(problem, result.plan)
    assert checked.cash_cost == pytest.approx(-0.0000012, abs=1e-10, rel=0)
    assert checked.bonus_income == pytest.approx(0.0000015, abs=1e-10, rel=0)


@pytest.mark.parametrize("initial_offset,expected_income", [(0, 0.15), (7499.6, 0.02), (7501, 0)])
def test_realistic_annual_cap_matches_strict_independent_settlement(
    solver, initial_offset, expected_income
):
    """A 7500-kWh cap credits both exports, only the first 0.4 kWh, or none. Keep the existing 1e-7 validator."""
    problem = network((1, 1), (bonus((0.05, 0.10), (7500, 7500), initial_offset),))
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL
    checked = validate(problem, result.plan)
    assert checked.bonus_income == pytest.approx(expected_income, abs=1e-9, rel=0)


@pytest.mark.planner_scenario
@pytest.mark.parametrize("cap", [6000, 7500])
def test_192_step_cap_counts_early_unpaid_export_before_small_paid_exports(solver, cap):
    """First 150*2=300 kWh consume all quota at zero rate. The 42 tiny later exports earn no bonus."""
    volumes = (2,) * 150 + (0.00001,) * 42
    current = bonus((0,) * 150 + (0.1,) * 42, (cap,) * 192, cap - 300)
    problem = network(volumes, (current,))
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL
    checked = validate(problem, result.plan)
    assert checked.bonus_income == pytest.approx(0, abs=1e-10, rel=0)
    assert result.plan.export_bonus_results[0].quota_kwh[150:] == pytest.approx(
        (0,) * 42, abs=1e-8, rel=0
    )


def test_near_zero_selector_cannot_admit_unearned_credit():
    """Exclude a forced 1.2e-6-kWh credit from native inequalities after the annual quota is exhausted.

    Normal 192-step solves currently return exact selectors. This witness instead
    perturbs one selector to 5e-9, within integrality tolerance, while preserving
    physical routes and counters. The constraint system must reject the false
    credit. This does not claim the normal MIP returned the perturbed values.
    """
    from math import fsum

    from open_energy_planner.optimization.adapters.highs.solver import NATIVE_OPTIONS

    volumes = (2,) * 150 + (0.00001,) * 42
    problem = network(volumes, (bonus((0,) * 150 + (0.1,) * 42, (7500,) * 192, 7200),))
    model = InstallationModelFactory().build(problem)
    native = model.context.native
    for key, value in NATIVE_OPTIONS.items():
        native.setOptionValue(key, value)
    native.run()
    assert native.getModelStatus() == highspy.HighsModelStatus.kOptimal
    validate(problem, model.extract())
    values = list(native.getSolution().col_value)
    variables = model.context.binding("export_bonus:extra").variables
    for name, value in (
        ("positive", 5e-9),
        ("remaining", 1.2e-6),
        ("credit", 1.2e-6),
        ("minimum", 0),
    ):
        values[int(variables[name][151])] = value
    violations = []
    for row in range(native.getNumRow()):
        _status, lower, upper, _count = native.getRow(row)
        _status, indices, coefficients = native.getRowEntries(row)
        actual = fsum(
            values[int(index)] * float(coefficient)
            for index, coefficient in zip(indices, coefficients, strict=True)
        )
        violations.append(max(lower - actual, actual - upper, 0))
    assert max(violations) > 1e-7


def test_battery_and_pv_share_aggregate_quota(solver):
    """Export 0.4 PV plus 0.6 stored kWh. Only 0.75 earns 0.04, yielding 0.03 bonus."""
    problem = replace(
        network((0.4,), (bonus((0.04,), (0.75,)),), (0.10,)),
        inverters=(
            SolarInverter("pv_inverter", 8000, 1),
            SolarInverter("battery_inverter", 2400, 1),
        ),
        batteries=(
            BatteryState(
                "battery",
                "battery_inverter",
                Battery(1, 0.6, charge_limit_w=0, discharge_limit_w=2400),
            ),
        ),
    )
    result = solver.solve(problem)
    costs = validate(problem, result.plan)
    assert result.plan.flows[0].batteries[0].discharge_kwh == pytest.approx(0.6)
    assert costs.export_income == pytest.approx(0.10)
    assert costs.bonus_income == pytest.approx(0.03)


@pytest.mark.planner_scenario
def test_every_discrete_choice_and_global_optimum_have_independent_oracle(solver):
    """Enumerate all eight on/off schedules using rational prefix intersections, then prove optimum -0.06.

    One kWh quota remains. Exporting only at t1 and t2 earns (0.10-0.05)+0.01.
    Spending quota at t0 earns only 0.02-0.01. Every schedule, including bad
    choices, must settle correctly before comparison with the native optimum.
    """
    rates = tuple(map(Fraction, (".02", ".10", ".04")))
    sales = tuple(map(Fraction, ("-.01", "-.05", ".01")))
    rule = bonus(tuple(map(float, rates)), (1.5,) * 3, 0.5)
    problem = network((1, 1, 1), (rule,), tuple(map(float, sales)), curtail=True)
    outcomes = []
    for chosen in product((0, 1), repeat=3):
        prefixes = [Fraction(".5")]
        for volume in chosen:
            prefixes.append(prefixes[-1] + volume)
        quota = tuple(
            min(Fraction("1.5"), right) - min(Fraction("1.5"), left)
            for left, right in pairwise(prefixes)
        )
        expected = -sum(
            price * volume + rate * credit
            for price, volume, rate, credit in zip(sales, chosen, rates, quota, strict=True)
        )
        flows = tuple(
            InstallationFlow(
                0,
                volume,
                (),
                (InverterFlow("pv_inverter", 0, volume, False),),
                (PVFlow("pv", volume, bool(volume)),),
            )
            for volume in chosen
        )
        sale_income = sum(price * volume for price, volume in zip(sales, chosen, strict=True))
        bonus_income = sum(rate * credit for rate, credit in zip(rates, quota, strict=True))
        costs = Costs(
            0, float(sale_income), float(bonus_income), 0, float(expected), 0, float(expected)
        )
        credit = ExportBonusResult(
            "extra", tuple(map(float, quota)), tuple(map(float, prefixes[1:]))
        )
        checked = validate(problem, InstallationPlan(flows, costs, (credit,)))
        assert checked.cash_cost == pytest.approx(float(expected))
        outcomes.append(expected)
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL
    assert result.plan.costs.objective == pytest.approx(float(min(outcomes)))
    assert min(outcomes) == Fraction("-.06")
    assert [flow.grid_export for flow in result.plan.flows] == pytest.approx((0, 1, 1))


@pytest.mark.parametrize("fault", ["quota", "counter", "identity", "income", "missing", "nan"])
def test_forged_bonus_outputs_are_rejected(solver, fault):
    """Reject forged native results independently, including a plausible but unearned extra payment."""
    problem = network((1,), (bonus((0.05,), (0.4,)),))
    plan = solver.solve(problem).plan
    validate(problem, plan)
    credit = plan.export_bonus_results[0]
    if fault == "quota":
        plan = replace(plan, export_bonus_results=(replace(credit, quota_kwh=(1,)),))
    elif fault == "counter":
        plan = replace(plan, export_bonus_results=(replace(credit, counters_kwh=(0.4,)),))
    elif fault == "identity":
        plan = replace(plan, export_bonus_results=(replace(credit, rule_id="other"),))
    elif fault == "income":
        plan = replace(plan, costs=replace(plan.costs, bonus_income=0.05))
    elif fault == "missing":
        plan = replace(plan, export_bonus_results=())
    else:
        plan = replace(plan, export_bonus_results=(replace(credit, quota_kwh=(float("nan"),)),))
    expected_issue = {
        "quota": "Bonus settlement",
        "counter": "Bonus settlement",
        "identity": "Bonus result shape",
        "income": "Financial totals",
        "missing": "Missing bonus evidence",
        "nan": "Bonus settlement",
    }[fault]
    with pytest.raises(ValueError, match=f"^{expected_issue}$"):
        validate(problem, plan)
