"""Independent period arithmetic for frozen flows, inventory and chronological bonus limits."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta

import pytest
from aiohttp import ClientSession

from open_energy_planner.savings.forecast import comparison
from open_energy_planner.savings.scenarios import scenarios
from open_energy_planner.savings.windows import window_summary
from tests.app.savings.support.scenarios import START, problem


def window_plan():
    """Charge 1 kWh at .30, then export .5 at .10. Stock rises from 2 to 2.5 at .20/kWh."""
    physics = problem(load_kwh=0)
    first = replace(physics.intervals[0], duration_h=1, sell_price=0.1)
    physics = replace(
        physics,
        intervals=(first, replace(first, start=START + timedelta(hours=1), duration_h=0.5)),
        batteries=(
            replace(
                physics.batteries[0], battery=replace(physics.batteries[0].battery, minimum_kwh=1)
            ),
        ),
        terminal_valuations=(replace(physics.terminal_valuations[0], reserve_kwh=1, usable_kwh=2),),
    )
    return {
        "input_snapshot": {
            "problem": json.loads(
                json.dumps(asdict(physics), default=lambda value: value.isoformat())
            )
        },
        "expected_flows": [
            {
                "grid_import": 1,
                "grid_export": 0,
                "batteries": [{"device_id": "battery", "end_kwh": 3, "discharge_kwh": 0}],
            },
            {
                "grid_import": 0,
                "grid_export": 0.5,
                "batteries": [{"device_id": "battery", "end_kwh": 2.5, "discharge_kwh": 0.5}],
            },
        ],
        "independent_costs": {
            "import_cost": 0.3,
            "export_income": 0.05,
            "bonus_income": 0,
            "wear_cost": 0,
            "cash_cost": 0.25,
            "terminal_value": 0.3,
        },
    }


def test_full_window_equals_original_comparison_without_mutation():
    """.30 purchase minus .05 sale minus .10 added inventory gives .15 economic cost, exactly as before."""
    plan = window_plan()
    before = deepcopy(plan)
    reference = comparison(plan)
    result = window_summary(plan, scenarios(plan, reference), START, START + timedelta(hours=48))
    costs = result["scenarios"]["optimizer"]["costs"]
    assert costs["purchase"] == pytest.approx(0.3)
    assert costs["sale"] == pytest.approx(0.05)
    assert costs["inventory_change"] == pytest.approx(0.1)
    assert costs["economic_cost"] == pytest.approx(0.15)
    assert costs["economic_cost"] == pytest.approx(reference["ems_costs"]["economic_cost"])
    assert result["period"]["duration_h"] == 1.5
    assert result["valuation_basis"] == "original_plan"
    assert plan == before


def test_partial_mixed_intervals_use_both_endpoint_stocks_and_add_up():
    """Half-hour charging and quarter-hour discharge cost .15-.025-.05=.075, not half the whole result."""
    plan = window_plan()
    refs = scenarios(plan, comparison(plan))
    start, stop = START + timedelta(minutes=30), START + timedelta(hours=1, minutes=15)
    costs = window_summary(plan, refs, start, stop)["scenarios"]["optimizer"]["costs"]
    assert costs["electricity_cost"] == pytest.approx(0.125)
    assert costs["inventory_change"] == pytest.approx(0.05)
    assert costs["economic_cost"] == pytest.approx(0.075)
    pieces = [
        window_summary(plan, refs, a, b)["scenarios"]["optimizer"]["costs"]
        for a, b in ((START, start), (start, stop), (stop, START + timedelta(hours=1.5)))
    ]
    assert sum(item["economic_cost"] for item in pieces) == pytest.approx(0.15)


def test_inventory_keeps_reserve_useful_volume_and_original_rate():
    """A .5-kWh useful cap already values the opening 2 kWh at .10, so further charging adds no value."""
    plan = window_plan()
    plan["input_snapshot"]["problem"]["terminal_valuations"][0]["usable_kwh"] = 0.5
    costs = window_summary(plan, {}, START, START + timedelta(hours=1))["scenarios"]["optimizer"][
        "costs"
    ]
    assert costs["inventory_change"] == 0
    assert costs["economic_cost"] == pytest.approx(0.3)


def test_two_batteries_keep_independent_stock_and_valuation_curves():
    """One added kWh in each battery is worth .20 plus .40, offsetting .60 purchase without merging identities."""
    plan = window_plan()
    data = plan["input_snapshot"]["problem"]
    battery = deepcopy(data["batteries"][0])
    battery["device_id"] = "second"
    battery["battery"].update(initial_kwh=1, minimum_kwh=0)
    data["batteries"].append(battery)
    curve = deepcopy(data["terminal_valuations"][0])
    curve.update(device_id="second", reserve_kwh=0, usable_kwh=4, rate=0.4)
    data["terminal_valuations"].append(curve)
    plan["expected_flows"][0]["grid_import"] = 2
    for flow in plan["expected_flows"]:
        flow["batteries"].append({"device_id": "second", "end_kwh": 2, "discharge_kwh": 0})
    costs = window_summary(plan, {}, START, START + timedelta(hours=1))["scenarios"]["optimizer"][
        "costs"
    ]
    assert costs["purchase"] == pytest.approx(0.6)
    assert costs["inventory_change"] == pytest.approx(0.6)
    assert costs["economic_cost"] == pytest.approx(0)


def test_discharge_losses_and_wear_keep_original_economic_meaning():
    """.5-kWh discharge at .8 efficiency costs .125 wear and leaves .075 added stock value: .25+.125-.075=.30."""
    plan = window_plan()
    battery = plan["input_snapshot"]["problem"]["batteries"][0]["battery"]
    battery.update(discharge_efficiency=0.8, cycle_fee=0.8)
    plan["expected_flows"][1]["batteries"][0]["end_kwh"] = 2.375
    costs = window_summary(plan, {}, START, START + timedelta(hours=2))["scenarios"]["optimizer"][
        "costs"
    ]
    assert costs["wear"] == pytest.approx(0.125)
    assert costs["inventory_change"] == pytest.approx(0.075)
    assert costs["economic_cost"] == pytest.approx(0.3)


def test_bonus_room_is_consumed_before_window_and_within_partial_interval():
    """Earlier 2-kWh export leaves 1 kWh of a 3-kWh cap. Only the first half of the next 2-kWh slot earns .10."""
    plan = window_plan()
    data = plan["input_snapshot"]["problem"]
    data["batteries"] = []
    data["terminal_valuations"] = []
    data["intervals"][1]["duration_h"] = 1
    data["export_bonuses"] = [
        {
            "rule_id": "bonus",
            "initial_kwh": 0,
            "rates": [0.1, 0.1],
            "caps_kwh": [3, 3],
            "periods": ["period", "period"],
        }
    ]
    plan["expected_flows"] = [
        {"grid_import": 0, "grid_export": 2, "batteries": []} for _ in range(2)
    ]
    early = window_summary(plan, {}, START + timedelta(hours=1), START + timedelta(hours=1.5))
    late = window_summary(plan, {}, START + timedelta(hours=1.5), START + timedelta(hours=2))
    assert early["scenarios"]["optimizer"]["costs"]["bonus"] == pytest.approx(0.1)
    assert late["scenarios"]["optimizer"]["costs"]["bonus"] == 0
    data["export_bonuses"][0]["periods"][1] = "next-period"
    reset = window_summary(plan, {}, START + timedelta(hours=1.5), START + timedelta(hours=2))
    assert reset["scenarios"]["optimizer"]["costs"]["bonus"] == pytest.approx(0.1)


def test_missing_reference_is_independent_and_never_invents_a_benefit():
    """A valid .15 optimizer amount survives missing baseline evidence, with an unknown benefit."""
    result = window_summary(window_plan(), {}, START, START + timedelta(hours=2))
    assert result["scenarios"]["optimizer"]["costs"]["economic_cost"] == pytest.approx(0.15)
    assert result["scenarios"]["optimizer"]["benefit_vs_without_battery"] is None
    assert "costs" not in result["scenarios"]["without_battery"]


@pytest.mark.parametrize("damage", ["missing_flow", "missing_stock", "nan_grid"])
def test_incomplete_optimizer_evidence_stays_unavailable(damage):
    """Missing flow/state or nonfinite grid energy cannot turn into a zero cost."""
    plan = window_plan()
    if damage == "missing_flow":
        plan["expected_flows"].pop()
    elif damage == "missing_stock":
        plan["expected_flows"][0]["batteries"] = []
    else:
        plan["expected_flows"][0]["grid_import"] = float("nan")
    result = window_summary(plan, {}, START, START + timedelta(hours=2))
    assert "costs" not in result["scenarios"]["optimizer"]
    assert result["scenarios"]["optimizer"]["issue"]


@pytest.mark.parametrize(
    "start,end",
    [(START, START), (START + timedelta(hours=1), START), (START.replace(tzinfo=None), START)],
)
def test_invalid_window_is_rejected(start, end):
    """Empty, reversed and timezone-free requests do not become a different financial period."""
    with pytest.raises(ValueError, match="timezone-aware"):
        window_summary(window_plan(), {}, start, end)


def test_expired_plan_has_no_remaining_costs_instead_of_zero():
    """A visible window entirely after the original plan shows no coverage and no fabricated saving."""
    result = window_summary(window_plan(), {}, START + timedelta(days=1), START + timedelta(days=2))
    assert result["period"] is None
    assert all("costs" not in entry for entry in result["scenarios"].values())


def test_window_http_authentication_validation_and_read_only_totals(tmp_path):
    """Authenticated slices return .075 without rewriting the archive. Bad dates and anonymous requests fail safely."""
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
    from tests.app.contract.test_contract_app import running

    plan = {
        **window_plan(),
        "plan_id": "window-test",
        "generated_at": START.isoformat(),
        "currency": "EUR",
    }
    repository = SqlPlanRepository(tmp_path)
    try:
        repository.state["plan"] = plan
        repository.save()
    finally:
        repository.close()

    async def check():
        """Read the real authenticated route with both valid and invalid period requests."""
        async with running(tmp_path) as (session, url):
            path = url + "/v1/plans/window-test/comparison"
            query = {
                "start": (START + timedelta(minutes=30)).isoformat(),
                "end": (START + timedelta(minutes=75)).isoformat(),
            }
            async with ClientSession() as anonymous, anonymous.get(path, params=query) as response:
                assert response.status == 401
            async with session.get(path, params=query) as response:
                assert response.status == 200
                body = await response.json()
                assert body["scenarios"]["optimizer"]["costs"]["economic_cost"] == pytest.approx(
                    0.075
                )
                assert body["period"]["duration_h"] == 0.75
                assert "plan" not in body
            for invalid in (
                {"start": "private-sentinel"},
                {"start": START.isoformat(), "end": START.isoformat()},
                {"start": START.replace(tzinfo=None).isoformat(), "end": query["end"]},
            ):
                async with session.get(path, params=invalid) as response:
                    assert response.status == 400
                    assert (
                        await response.text()
                        == "Choose an increasing, timezone-aware planning period"
                    )
            async with session.get(url + "/v1/plans/window-test") as response:
                assert await response.json() == plan

    asyncio.run(check())
