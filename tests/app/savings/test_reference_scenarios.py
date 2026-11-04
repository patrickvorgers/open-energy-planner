"""Independent expected reference arithmetic and replay invariants for the plan explanation UI."""

import json
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from open_energy_planner.savings.forecast import comparison
from open_energy_planner.savings.scenarios import scenario, scenarios
from tests.app.savings.support.scenarios import problem


def plan_document(physics):
    """Retain a zero-cash EMS expectation with unchanged stock, without invoking any optimizer."""
    return {
        "input_snapshot": {
            "problem": json.loads(
                json.dumps(asdict(physics), default=lambda value: value.isoformat())
            )
        },
        "independent_costs": {
            "cash_cost": 0,
            "terminal_value": 0.4 if physics.batteries else 0,
            "import_cost": 0,
            "export_income": 0,
            "bonus_income": 0,
            "wear_cost": 0,
        },
    }


def test_without_battery_exports_even_when_export_costs_money():
    """1 kWh solar minus .5 demand exports .5 at -.1: .05 cost. Self-consumption stores .5 instead."""
    plan = plan_document(problem(pv_kwh=1, load_kwh=0.5))
    before = deepcopy(plan)
    result = scenarios(plan, comparison(plan))
    bare = result["without_battery"]
    stored = result["self_consumption"]
    assert bare["comparison"]["reference_costs"]["economic_cost"] == pytest.approx(0.05)
    assert bare["comparison"]["expected_savings"] == pytest.approx(0.05)
    assert bare["chart"]["expected_flows"][0]["grid_export"] == pytest.approx(0.5)
    assert bare["chart"]["input_snapshot"]["problem"]["batteries"] == []
    assert stored["chart"]["expected_flows"][0]["grid_export"] == 0
    assert stored["chart"]["expected_flows"][0]["batteries"][0]["end_kwh"] == 2.5
    assert result["self_consumption"]["expected_savings_vs_without_battery"] == pytest.approx(0.15)
    assert plan == before


@pytest.mark.parametrize("efficiency", [1, 0.8])
@pytest.mark.parametrize("pv,load", [(0, 1), (1, 0), (4, 0), (1, 0.5)])
def test_reference_charts_conserve_energy_with_losses_and_clipping(efficiency, pv, load):
    """For both references, DC PV plus import/discharge equals load/export/charge plus declared converter losses."""
    plan = plan_document(problem(pv_kwh=pv, load_kwh=load, efficiency=efficiency))
    result = scenarios(plan, comparison(plan))
    for entry in result.values():
        assert "issue" not in entry
        flow = entry["chart"]["expected_flows"][0]
        solar = sum(source["used_kwh"] for source in flow["solar"])
        charged = sum(unit["charge_kwh"] for unit in flow["batteries"])
        discharged = sum(unit["discharge_kwh"] for unit in flow["batteries"])
        losses = sum(
            converter["import_kwh"] * (1 - efficiency)
            + converter["export_kwh"] * (1 / efficiency - 1)
            for converter in flow["inverters"]
        )
        assert solar + flow["grid_import"] + discharged == pytest.approx(
            load + flow["grid_export"] + charged + losses
        )
        assert solar <= pv


def test_legacy_replay_requires_the_retained_reference_outcomes():
    """A changed historical reference outcome must produce an explicit gap, never plausible replacement bars."""
    plan = plan_document(problem())
    reference = comparison(plan)
    reference["reference"][0]["import_kwh"] = 99
    with pytest.raises(ValueError, match="cannot be reproduced"):
        scenario(plan, reference)
    assert "issue" in scenarios(plan, reference)["self_consumption"]
    assert "issue" not in scenarios(plan, reference)["without_battery"]


def test_continuous_reference_start_is_not_reset_to_actual_soc():
    """Reference starts empty while the actual battery has 2 kWh. It imports 1 kWh instead of discharging."""
    plan = plan_document(problem())
    reference = comparison(plan, {"stored": {"battery": 0}, "counters": {}})
    chart = scenario(plan, reference)["chart"]
    assert chart["input_snapshot"]["problem"]["batteries"][0]["battery"]["initial_kwh"] == 0
    assert chart["expected_flows"][0]["grid_import"] == 1


@pytest.mark.parametrize("ems_cash,expected_saving", [(0, 0.2), (0.3, -0.1)])
def test_future_choice_uses_measured_stock_and_values_remaining_energy(ems_cash, expected_saving):
    """Self-consumption spends 1 kWh worth .20. EMS keeps its stock and costs either zero or .30."""
    plan = plan_document(problem())
    plan["independent_costs"]["cash_cost"] = ems_cash
    plan["independent_costs"]["import_cost"] = ems_cash
    original = deepcopy(plan)
    reference = comparison(plan)
    chart = scenario(plan, reference)["chart"]
    assert reference["reference_start"]["status"] == "measured_common_start"
    assert chart["input_snapshot"]["problem"]["batteries"][0]["battery"]["initial_kwh"] == 2
    assert chart["expected_flows"][0]["batteries"][0]["end_kwh"] == 1
    assert reference["reference_costs"]["purchase"] == 0
    assert reference["reference_costs"]["inventory_change"] == pytest.approx(-0.2)
    assert reference["expected_savings"] == pytest.approx(expected_saving)
    assert plan == original


def test_without_battery_respects_bonus_cap_and_retains_original_budget():
    """One exported kWh has only .25 quota left: .2 bonus rate pays .05, after .1 export cost."""
    plan = plan_document(problem(pv_kwh=1, load_kwh=0))
    plan["input_snapshot"]["problem"]["export_bonuses"] = [
        {
            "rule_id": "bonus",
            "rates": [0.2],
            "caps_kwh": [2],
            "periods": ["year"],
            "initial_kwh": 1.75,
        }
    ]
    reference = comparison(plan, without_battery=True)
    assert reference["reference_costs"]["bonus"] == pytest.approx(0.05)
    assert reference["reference_costs"]["economic_cost"] == pytest.approx(0.05)


def test_without_battery_grid_limit_issue_does_not_break_self_consumption():
    """A zero export limit is feasible with storage absorbing 1 kWh, but impossible without it. Never clip for price."""
    plan = plan_document(replace(problem(pv_kwh=1, load_kwh=0), grid_export_limit_w=0))
    result = scenarios(plan, comparison(plan))
    assert "grid limit" in result["without_battery"]["issue"]
    assert "issue" not in result["self_consumption"]


def test_unknown_bonus_budget_preserves_physical_reference_but_not_costs():
    """An empty continuous battery imports 1 kWh. Missing historical quota cannot hide bars or become a zero bonus."""
    plan = plan_document(problem())
    plan["input_snapshot"]["problem"]["export_bonuses"] = [
        {
            "rule_id": "bonus",
            "rates": [0.2],
            "caps_kwh": [2],
            "periods": ["year"],
            "initial_kwh": 1.75,
        }
    ]
    original = deepcopy(plan)
    reference = comparison(plan, {"stored": {"battery": 0}, "counters": {}})
    assert reference["reference_costs"] is None
    assert reference["expected_savings"] is None
    assert reference["financial_issue"] == "Continuous reference bonus state is missing"
    result = scenarios(plan, reference)["self_consumption"]
    assert result["chart"]["expected_flows"][0]["grid_import"] == 1
    assert result["chart"]["expected_flows"][0]["batteries"][0]["end_kwh"] == 0
    assert result["issue"] == reference["financial_issue"]
    assert "expected_savings_vs_without_battery" not in result
    assert plan == original
