"""Frozen-flow contract correction scenarios. Expected amounts are independent of optimizer outputs."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from open_energy_planner.contracts.bonus import BonusRule, CounterBook, CounterSeed
from open_energy_planner.contracts.calculation.formula import Formula
from open_energy_planner.contracts.model import TariffEvidence
from open_energy_planner.savings.forecast import comparison
from open_energy_planner.savings.repricing import initial_counters, plan_activity_context, reprice
from tests.app.savings.support.scenarios import END, START, contract, problem


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        "invalid",
        {"problem": {"intervals": []}},
        {"problem": {"intervals": [{"start": "invalid", "duration_h": 0.25}]}},
        {"problem": {"intervals": [{"start": START.isoformat(), "duration_h": "invalid"}]}},
    ],
)
def test_plan_activity_context_does_not_obscure_invalid_archive_failure(snapshot):
    """Unusable horizon evidence keeps a safe subject without aborting failure bookkeeping."""
    assert plan_activity_context({"input_snapshot": snapshot}) == {"label": "Archived plan"}


def original_plan(*, solar=False):
    """A frozen grid-only or PV-export plan retains .10 wholesale and .30 retail expectation explicitly."""
    planning_problem = problem(pv_kwh=0.5 if solar else 0, load_kwh=0 if solar else 1)
    planning_problem = replace(planning_problem, batteries=(), terminal_valuations=())
    if not solar:
        planning_problem = replace(
            planning_problem,
            pv_sources=(),
            inverters=(),
            intervals=(replace(planning_problem.intervals[0], pv_sources=()),),
        )
    intervals = (
        (planning_problem.intervals[0], replace(planning_problem.intervals[0], start=END))
        if solar
        else planning_problem.intervals
    )
    planning_problem = replace(planning_problem, intervals=intervals)
    evidence = [
        asdict(
            TariffEvidence(
                interval.start,
                interval.start + timedelta(minutes=15),
                0.1,
                0.3,
                0,
                "published",
                15,
                "test-market",
                "original-wholesale",
                "original-contract",
                0,
            )
        )
        for interval in intervals
    ]
    data = {
        "plan_id": "repricing-source",
        "generated_at": START.isoformat(),
        "strategy": "minimize cost",
        "solution_status": "optimal",
        "currency": "EUR",
        "market_area": "NL",
        "input_snapshot": {
            "contract_version": "original-contract",
            "problem": asdict(planning_problem),
            "tariff_evidence": evidence,
        },
        "expected_flows": [
            {"grid_import": 0 if solar else 1, "grid_export": 0.5 if solar else 0, "batteries": []}
            for _ in intervals
        ],
        "independent_costs": {
            "import_cost": 0 if solar else 0.3,
            "export_income": 0,
            "bonus_income": 0,
            "wear_cost": 0,
            "cash_cost": 0 if solar else 0.3,
            "terminal_value": 0,
            "objective": 0 if solar else 0.3,
        },
        "steps": [],
    }
    return json.loads(json.dumps(data, default=lambda value: value.isoformat()))


def test_contract_repricing_changes_money_not_plan_or_market_knowledge():
    """One original import kWh changes from .30 to .40 retail cost. Original advice and wholesale .10 remain unchanged."""
    plan = original_plan()
    before = deepcopy(plan)
    result = reprice(plan, contract(buy="0.4"), None, END)
    assert result["ems_costs"]["purchase"] == pytest.approx(0.4)
    assert result["reference_costs"]["purchase"] == pytest.approx(0.4)
    assert result["expected_savings"] == 0
    assert plan == before
    assert plan["independent_costs"]["import_cost"] == 0.3
    assert plan["input_snapshot"]["tariff_evidence"][0]["market_price"] == 0.1


@pytest.mark.parametrize("timestamp_kind", ["ha_report", "ha_state", "device_sample"])
def test_archived_capture_keeps_unchanged_export_usable_without_retiming_original_samples(
    timestamp_kind,
):
    """An unchanged 10-kWh HA counter confirms 1.5-kWh prior use at capture, preserving all original provenance kinds."""
    plan = original_plan(solar=True)
    corrected = contract(bonus=True)
    period = corrected.bonuses[0].period_start(START)
    seed = CounterSeed("bonus", period, 1.5, 10, "sensor.export", START - timedelta(hours=1))
    book = CounterBook(START, (seed,))
    plan["input_snapshot"]["measured_at"] = START.isoformat()
    old = (START - timedelta(hours=2)).isoformat()
    plan["input_snapshot"]["measurement_evidence"] = [
        {
            "key": "grid.export_energy",
            "entity_id": "sensor.export",
            "value": 10,
            "observed_at": old,
            "reported_at": old,
            "timestamp_kind": timestamp_kind,
        }
    ]
    before = deepcopy(plan)
    actual, reference = initial_counters(plan, corrected, book, None)
    assert actual == reference == {"bonus|" + period.isoformat(): 1.5}
    assert plan == before


def test_corrected_bonus_gate_cannot_move_first_volume_to_later_profitable_slot():
    """Inherited use 1.5 plus first .5 export exhausts cap before the corrected 10:15 payment window opens."""
    plan = original_plan(solar=True)
    old = contract(bonus=True)
    period = old.bonuses[0].period_start(START).isoformat()
    plan["input_snapshot"]["bonus_evidence"] = [
        {"rule_id": "bonus", "period_start": period, "initial_kwh": 1.5}
    ]
    rule = BonusRule(
        "bonus",
        START - timedelta(days=1),
        Formula("0.2"),
        2,
        timezone="UTC",
        window="clock",
        start_time="10:15",
        end_time="11:00",
    )
    corrected = replace(contract(sell="0"), bonuses=(rule,))
    result = reprice(plan, corrected, None, END + timedelta(hours=1))
    assert result["ems_costs"]["bonus"] == 0
    assert result["reference_costs"]["bonus"] == 0
    key = "bonus|" + period
    assert result["initial_counters"]["ems"][key] == 1.5
    assert result["ending_counters"]["ems"][key] == 2.5


def test_future_reference_uses_its_own_state_and_does_not_borrow_actual_stored_energy():
    """Actual battery has two kWh while reference is empty. Reference must buy two kWh, yielding .20 incremental benefit."""
    planning_problem = problem()
    planning_problem = replace(
        planning_problem,
        intervals=(
            planning_problem.intervals[0],
            replace(planning_problem.intervals[0], start=END),
        ),
    )
    plan = {
        "input_snapshot": {
            "problem": json.loads(
                json.dumps(asdict(planning_problem), default=lambda value: value.isoformat())
            )
        },
        "independent_costs": {
            "import_cost": 0,
            "export_income": 0,
            "bonus_income": 0,
            "wear_cost": 0,
            "cash_cost": 0,
            "terminal_value": 0,
        },
    }
    result = comparison(
        plan,
        {
            "stored": {"battery": 0},
            "counters": {},
            "status": "continuous_reference",
            "estimated": False,
        },
    )
    assert sum(row["import_kwh"] for row in result["reference"]) == 2
    assert result["expected_savings"] == pytest.approx(0.2)


def test_currency_change_and_missing_original_wholesale_are_not_fabricated():
    """USD cannot reinterpret EUR wholesale. Absent original market evidence cannot be replaced by retail prices."""
    with pytest.raises(ValueError, match="currency"):
        reprice(original_plan(), replace(contract(), currency="USD"), None, END)
    plan = original_plan()
    plan["input_snapshot"].pop("tariff_evidence")
    with pytest.raises(ValueError, match="wholesale"):
        reprice(plan, contract(), None, END)


def test_repricing_preserves_wholesale_evidence_beyond_command_horizon():
    """A retained .90 later forecast becomes 1.00 effective value, rather than the .20 in-horizon proxy."""
    planning_problem = problem()
    data = original_plan()
    data["input_snapshot"]["problem"] = json.loads(
        json.dumps(asdict(planning_problem), default=lambda value_item: value_item.isoformat())
    )
    later = TariffEvidence(
        END,
        END + timedelta(hours=1),
        0.9,
        1,
        0,
        "forecast",
        60,
        "forecast-provider",
        "original-future",
        "original-contract",
        0,
    )
    curve = data["input_snapshot"]["problem"]["terminal_valuations"][0]
    curve.update(
        price_basis="post_horizon_prices",
        window_start=END.isoformat(),
        window_end=(END + timedelta(hours=1)).isoformat(),
        tariff_evidence=json.loads(
            json.dumps([asdict(later)], default=lambda value_item: value_item.isoformat())
        ),
    )
    data["expected_flows"] = [
        {"grid_import": 0, "grid_export": 0, "batteries": [{"device_id": "battery", "end_kwh": 1}]}
    ]
    result = reprice(data, contract(buy="market + 0.1", sell="0"), None, END)
    assert result["terminal_values"][0]["price_basis"] == "post_horizon_prices"
    assert result["terminal_values"][0]["rate"] == pytest.approx(1)
    assert (
        data["input_snapshot"]["problem"]["terminal_valuations"][0]["tariff_evidence"][0][
            "market_price"
        ]
        == 0.9
    )


def test_bonus_introduced_mid_plan_does_not_require_pre_activation_seed():
    """Only the second 0.5-kWh export earns 0.015 EUR. The first predates the bonus and needs no baseline."""
    plan = original_plan(solar=True)
    before = deepcopy(plan)
    selected = replace(
        contract(buy="0.3", sell="0.1"),
        bonuses=(BonusRule("new-bonus", END, Formula("0.03"), 5000, timezone="UTC"),),
    )
    assert initial_counters(plan, selected, None, None) == ({}, {})
    result = reprice(plan, selected, None, END + timedelta(minutes=15))
    assert result["ems_costs"]["bonus"] == pytest.approx(0.015)
    assert plan == before
