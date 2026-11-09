"""Budget-style windows, introductory counters and independent battery-export protection."""

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from open_energy_planner.contracts.bonus import BonusRule
from open_energy_planner.contracts.calculation.formula import Formula
from open_energy_planner.contracts.compilation import BonusCompiler
from open_energy_planner.contracts.windows import ClockWindow
from open_energy_planner.contracts.wire import contract_document, parse_contract
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import Battery, Interval
from open_energy_planner.optimization.export_bonus import ExportBonus, settle
from tests.app.contract.support.settings import settings
from tests.app.installation.support.problems import installation_problem

ZONE = ZoneInfo("Europe/Amsterdam")
START = datetime(2027, 1, 15, 12, tzinfo=ZONE)


def test_introduction_excludes_old_exports_without_moving_contract_anniversary():
    """January starts the first allowance. May still starts the next full supplier year."""
    rule = BonusRule(
        "budget",
        datetime(2027, 1, 1, tzinfo=ZONE),
        Formula("0.30 * min(max(market, 0), 0.20)"),
        5000,
        reset_month=5,
        reset_day=12,
        contract_start=datetime(2026, 5, 12, tzinfo=ZONE),
    )
    assert rule.period_start(START) == datetime(2027, 1, 1, tzinfo=ZONE)
    assert rule.period_start(datetime(2027, 5, 12, tzinfo=ZONE)) == datetime(
        2027, 5, 12, tzinfo=ZONE
    )


@pytest.mark.parametrize(
    "restricted,load,pv,export,discharge",
    [
        (True, 3, 1, 0, 2),
        (True, 1, 3, 2, 0),
        (True, 0, 0, 0, 0),
        (False, 0, 0, 4, 4),
    ],
)
def test_bonus_protection_allows_only_self_consumption(restricted, load, pv, export, discharge):
    """An ideal full battery covers residual demand, but cannot add export during a protected slot."""
    bonus = ExportBonus(
        "budget", (0.03,), (5000,), ("year",), 0, prevent_battery_export=(restricted,)
    )
    problem = installation_problem(
        (Interval(START, 1, 1, 0.1, load_kwh=load, pv_kwh=pv),),
        Battery(10, 10, charge_limit_w=4000, discharge_limit_w=4000),
        export_bonuses=(bonus,),
    )
    result = HighsSolver().solve(problem)
    validate(problem, result.plan)
    flow = result.plan.flows[0]
    assert flow.grid_export == pytest.approx(export)
    assert flow.batteries[0].discharge_kwh == pytest.approx(discharge)


def test_independent_validator_rejects_export_from_battery_under_protected_terms():
    """An otherwise feasible battery export is rejected even with correct physical and financial totals."""
    problem = installation_problem(
        (Interval(START, 1, 1, 0.1),),
        Battery(10, 10),
        export_bonuses=(ExportBonus("budget", (0.03,), (5000,), ("year",), 0),),
    )
    plan = HighsSolver().solve(problem).plan
    protected = replace(
        problem,
        export_bonuses=(replace(problem.export_bonuses[0], prevent_battery_export=(True,)),),
    )
    with pytest.raises(ValueError, match="Battery export during protected bonus window"):
        validate(protected, plan)


def dated_contract():
    """Keep a June supplier contract while adding its first bonus in January and new terms in March."""
    body = settings(sell="market")
    first = {
        **body["periods"][0],
        "contract_start": "2026-06-01T00:00:00+02:00",
        "effective_from": "2026-06-01T00:00:00+02:00",
        "effective_until": "2027-01-01T00:00:00+01:00",
    }
    second = {
        **first,
        "effective_from": first["effective_until"],
        "effective_until": "2027-03-01T00:00:00+01:00",
    }
    third = {**second, "effective_from": second["effective_until"], "effective_until": None}
    body["periods"] = [first, second, third]
    body["bonuses"] = [
        {
            "effective_from": period["effective_from"],
            "rate_formula": rate,
            "cap_kwh": 5000,
            "reset_month": 6,
            "reset_day": 1,
            "prevent_battery_export": True,
        }
        for period, rate in ((second, "0.03"), (third, "0.06"))
    ]
    return parse_contract(body, START)


def test_rule_versions_keep_first_bonus_activation_and_contract_reset():
    """The March rate change keeps January's first allowance and June's next annual reset."""
    contract = dated_contract()
    family = next(iter(BonusCompiler().families(contract).values()))
    assert family[0].rule_id == family[1].rule_id
    assert family[0].period_start(datetime(2027, 4, 1, tzinfo=ZONE)) == datetime(
        2027, 1, 1, tzinfo=ZONE
    )
    assert family[0].period_start(datetime(2027, 6, 1, tzinfo=ZONE)) == datetime(
        2027, 6, 1, tzinfo=ZONE
    )
    document = contract_document(contract)
    for key in ("status", "version", "recorded_at", "preview", "bonus_preview"):
        document.pop(key)
    assert parse_contract(document, START) == contract


def test_future_first_bonus_ignores_earlier_export_without_requiring_an_old_seed():
    """Ten December kWh earn no bonus or quota. January's first kWh earns 0.03 EUR with no old baseline."""
    contract = dated_contract()
    now = datetime(2026, 12, 31, 23, tzinfo=ZONE)
    prices = tuple(
        SimpleNamespace(
            start=now + timedelta(hours=index),
            end=now + timedelta(hours=index + 1),
            market_price=0.10,
        )
        for index in range(2)
    )
    rules, evidence = BonusCompiler().compile(contract, (None, None), prices, now, None, None, None)
    assert evidence == ()
    assert rules[0].prevent_battery_export == (False, True)
    result = settle(rules[0], (10, 1))
    assert result.quota_kwh == (0, 1)
    assert sum(
        rate * volume for rate, volume in zip(rules[0].rates, result.quota_kwh)
    ) == pytest.approx(0.03)


@pytest.mark.parametrize("value", ["true", 1, None, []])
def test_battery_export_permission_rejects_nonboolean_wire_values(value):
    """Ambiguous input cannot silently switch an operational planning restriction on or off."""
    contract = dated_contract()
    document = contract_document(contract)
    for key in ("status", "version", "recorded_at", "preview", "bonus_preview"):
        document.pop(key)
    document["bonuses"][0]["prevent_battery_export"] = value
    with pytest.raises(ValueError, match="must be boolean"):
        parse_contract(document, START)


@pytest.mark.parametrize(
    "rate,initial,cap,export",
    [(0, 0, 5000, 0), (0.03, 5000, 5000, 4), (0.03, 5001, 5000, 4), (0.03, 0, 0, 4)],
)
def test_protection_releases_only_when_quota_is_exhausted(rate, initial, cap, export):
    """Zero-rate hours remain protected with quota. Exhausted, exceeded and zero caps permit export."""
    rule = ExportBonus(
        "budget", (rate,), (cap,), ("year",), initial, prevent_battery_export=(True,)
    )
    problem = installation_problem(
        (Interval(START, 1, 1, 1),), Battery(10, 10), export_bonuses=(rule,)
    )
    plan = HighsSolver().solve(problem).plan
    validate(problem, plan)
    assert plan.flows[0].grid_export == pytest.approx(export)
    assert plan.flows[0].batteries[0].discharge_kwh == pytest.approx(export)


@pytest.mark.parametrize(
    "periods,caps,expected_export",
    [(("year", "year"), (1, 1), 4), (("year", "next"), (1, 1), 0), (("year", "year"), (1, 2), 0)],
)
def test_pv_exhaustion_releases_next_interval_until_reset_or_cap_increase(
    periods, caps, expected_export
):
    """PV uses the last kWh. Battery export then resumes unless fresh quota opens."""
    rule = ExportBonus("budget", (0.03, 0.03), caps, periods, 0, (True, True))
    problem = installation_problem(
        (Interval(START, 1, 1, 0.1, pv_kwh=1), Interval(START + timedelta(hours=1), 1, 1, 1)),
        Battery(10, 10),
        export_bonuses=(rule,),
    )
    plan = HighsSolver().solve(problem).plan
    validate(problem, plan)
    assert plan.flows[0].grid_export == pytest.approx(1)
    assert plan.flows[0].batteries[0].discharge_kwh == pytest.approx(0)
    assert plan.flows[1].grid_export == pytest.approx(expected_export)
    assert plan.flows[1].batteries[0].discharge_kwh == pytest.approx(expected_export)


def test_battery_cannot_exhaust_last_quota_to_release_its_own_interval():
    """Even 0.001 remaining kWh protects the whole interval from profitable battery export."""
    rule = ExportBonus("budget", (0.03,), (5000,), ("year",), 4999.999, (True,))
    problem = installation_problem(
        (Interval(START, 1, 1, 1),), Battery(10, 10), export_bonuses=(rule,)
    )
    plan = HighsSolver().solve(problem).plan
    validate(problem, plan)
    assert plan.flows[0].grid_export == pytest.approx(0)


def test_other_bonus_with_remaining_quota_keeps_protection_active():
    """An exhausted rule cannot release protection required by another active rule."""
    rules = (
        ExportBonus("used", (0.03,), (1,), ("year",), 1, (True,)),
        ExportBonus("open", (0.03,), (1,), ("year",), 0, (True,)),
    )
    problem = installation_problem(
        (Interval(START, 1, 1, 1),), Battery(10, 10), export_bonuses=rules
    )
    plan = HighsSolver().solve(problem).plan
    validate(problem, plan)
    assert plan.flows[0].grid_export == pytest.approx(0)


@pytest.mark.parametrize(
    "periods,caps,rejected",
    [
        (("year", "year"), (1, 1), False),
        (("year", "next"), (1, 1), True),
        (("year", "year"), (1, 2), True),
    ],
)
def test_validator_uses_prior_export_and_reopened_quota(periods, caps, rejected):
    """Independent settlement permits exhausted export but rejects a plan bypassing a new allowance."""
    rule = ExportBonus("budget", (0.03, 0.03), caps, periods, 0, (True, False))
    problem = installation_problem(
        (Interval(START, 1, 1, 0.1, pv_kwh=1), Interval(START + timedelta(hours=1), 1, 1, 1)),
        Battery(10, 10),
        export_bonuses=(rule,),
    )
    plan = HighsSolver().solve(problem).plan
    assert plan.flows[0].grid_export == pytest.approx(1)
    assert plan.flows[1].grid_export == pytest.approx(4)
    protected = replace(
        problem, export_bonuses=(replace(rule, prevent_battery_export=(True, True)),)
    )
    if rejected:
        with pytest.raises(ValueError, match="Battery export during protected bonus window"):
            validate(protected, plan)
    else:
        validate(protected, plan)


@pytest.mark.parametrize("load,pv,expected_export,expected_discharge", [(3, 1, 0, 2), (1, 3, 2, 0)])
def test_protection_applies_to_all_batteries_at_the_grid_boundary(
    load, pv, expected_export, expected_discharge
):
    """Two full batteries share the same residual load allowance and cannot add to PV export."""
    problem = installation_problem(
        (Interval(START, 1, 1, 0.1, load_kwh=load, pv_kwh=pv),),
        Battery(10, 10),
        export_bonuses=(ExportBonus("extra", (0.03,), (5000,), ("year",), 0, (True,)),),
    )
    problem = replace(
        problem, batteries=(problem.batteries[0], replace(problem.batteries[0], device_id="second"))
    )
    plan = HighsSolver().solve(problem).plan
    validate(problem, plan)
    assert plan.flows[0].grid_export == pytest.approx(expected_export)
    assert sum(battery.discharge_kwh for battery in plan.flows[0].batteries) == pytest.approx(
        expected_discharge
    )


def test_split_control_cannot_discharge_in_another_part_of_the_same_command():
    """Unavoidable PV export in the first half fixes battery power at zero for the complete original command."""
    intervals = (
        Interval(START, 0.5, 1, 0.1, pv_kwh=1, control_group="original"),
        Interval(START + timedelta(minutes=30), 0.5, 1, 0.1, load_kwh=1, control_group="original"),
    )
    problem = installation_problem(
        intervals,
        Battery(10, 10),
        export_bonuses=(
            ExportBonus("extra", (0.03, 0.03), (5000, 5000), ("year", "year"), 0, (True, True)),
        ),
    )
    plan = HighsSolver().solve(problem).plan
    validate(problem, plan)
    assert [flow.grid_export for flow in plan.flows] == pytest.approx([1, 0])
    assert [flow.grid_import for flow in plan.flows] == pytest.approx([0, 1])
    assert all(flow.batteries[0].discharge_kwh == pytest.approx(0) for flow in plan.flows)


def test_monthly_supplier_example_retains_all_twelve_windows_and_capped_rate():
    """Every published month opens and closes exactly. Positive price bonuses stop at 0.06 EUR/kWh."""
    schedule = (
        ("09:00", "17:00"),
        ("08:00", "18:00"),
        ("07:00", "19:00"),
        ("07:00", "21:00"),
        ("06:00", "21:00"),
        ("05:00", "22:00"),
        ("06:00", "22:00"),
        ("06:00", "21:00"),
        ("07:00", "20:00"),
        ("08:00", "19:00"),
        ("08:00", "17:00"),
        ("09:00", "17:00"),
    )
    rule = BonusRule(
        "example",
        START.replace(month=1, day=1, hour=0),
        Formula("0.30 * min(max(market, 0), 0.20)"),
        5000,
        window="monthly_schedule",
        monthly_schedule=tuple(ClockWindow(*window) for window in schedule),
    )
    for price, expected in ((-0.1, 0), (0, 0), (0.1, 0.03), (0.2, 0.06), (0.25, 0.06)):
        assert rule.extra_rate(price, START) == pytest.approx(expected)
    for month, (opening, closing) in enumerate(schedule, 1):
        start = datetime.fromisoformat(f"2027-{month:02}-15T{opening}").replace(tzinfo=ZONE)
        end = datetime.fromisoformat(f"2027-{month:02}-15T{closing}").replace(tzinfo=ZONE)
        assert rule.extra_rate(0.1, start - timedelta(seconds=1)) == 0
        assert rule.extra_rate(0.1, start) == pytest.approx(0.03)
        assert rule.extra_rate(0.1, end - timedelta(seconds=1)) == pytest.approx(0.03)
        assert rule.extra_rate(0.1, end) == 0


@pytest.mark.parametrize("mask", [[True], (1,), (None,), (True, False)])
def test_prepared_export_protection_rejects_ambiguous_or_unaligned_masks(mask):
    """Only immutable boolean flags aligned to actual billing segments may enter the solver."""
    with pytest.raises(ValueError, match="aligned booleans"):
        ExportBonus("extra", (0.03,), (5000,), ("year",), 0, mask)


def test_thermal_interval_refinement_preserves_protection_on_every_child_segment():
    """Each child of a protected hour retains the restriction, while the next unpaid hour stays unrestricted."""
    from open_energy_planner.appliances.thermal.service import refine

    rule = ExportBonus("extra", (0.03, 0), (5000, 5000), ("year", "year"), 0, (True, False))
    problem = installation_problem(
        (Interval(START, 1, 1, 0.1), Interval(START + timedelta(hours=1), 1, 1, 0.1)),
        export_bonuses=(rule,),
    )
    refined = refine(problem, START + timedelta(minutes=7))
    assert len(refined.intervals) == 9
    assert refined.export_bonuses[0].prevent_battery_export == (True,) * 5 + (False,) * 4


def test_budget_preview_uses_first_activation_after_a_later_price_rule():
    """April's budget keeps 99.6 measured kWh from January despite the March rate change."""
    from open_energy_planner.contracts.api import ContractAPI
    from tests.app.contract.test_contract_bonuses import measured_inputs

    now = datetime(2027, 4, 1, 12, tzinfo=ZONE)
    contract = dated_contract()
    config, report, book = measured_inputs(now=now)
    first, latest = contract.bonuses
    book = replace(
        book,
        seeds=(
            replace(
                book.seeds[0], rule_id=first.rule_id, period_start=datetime(2027, 1, 1, tzinfo=ZONE)
            ),
        ),
    )
    api = SimpleNamespace(
        contracts=SimpleNamespace(issue=None),
        bonus_counters=SimpleNamespace(issue=None),
        live=SimpleNamespace(configuration=config, report=report),
    )
    row = ContractAPI.bonus_budget_row(api, contract, latest, book, now)
    assert row["issue"] is None
    assert row["used_kwh"] == pytest.approx(99.6)
    assert row["remaining_kwh"] == pytest.approx(4900.4)
    assert datetime.fromisoformat(row["period_start"]) == datetime(2027, 1, 1, tzinfo=ZONE)
