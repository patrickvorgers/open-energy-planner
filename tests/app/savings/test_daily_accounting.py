"""Independent daily accounting preserves counter and inventory boundaries when another scenario is unavailable."""

from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.contracts.bonus import CounterSeed
from open_energy_planner.savings.accounting import independent_account, simulated_counters_ready
from open_energy_planner.savings.model import PendingSettlement
from tests.app.savings.support.scenarios import START, bindings, contract, frame, prices, problem


def test_new_retroactive_family_cannot_borrow_actual_seed_after_daily_closure():
    """An actual .50-kWh used allowance does not establish simulated exports before the recovery floor."""
    selected = contract(bonus=True)
    seed = CounterSeed("bonus", START - timedelta(days=1), 0.5, 10, "sensor.export", START)
    with pytest.raises(PendingSettlement, match="Simulated bonus counters are unknown"):
        independent_account(
            problem(), frame(), bindings(), "without_battery", {}, selected, (seed,), prices(), None
        )
    result, _counters = independent_account(
        problem(), frame(), bindings(), "actual", {}, selected, (seed,), prices(), None
    )
    assert result["purchase"] == pytest.approx(0.3)


def test_original_common_start_may_share_actual_opening_allowance_once():
    """Before scenarios diverge, the same established .50-kWh opening usage is valid for both."""
    selected = contract(bonus=True)
    seed = CounterSeed("bonus", START - timedelta(days=1), 0.5, 10, "sensor.export", START)
    _result, counters = independent_account(
        problem(),
        frame(),
        bindings(),
        "without_battery",
        {},
        selected,
        (seed,),
        prices(),
        None,
        common_start=True,
    )
    assert list(counters.values()) == [0.5]


def test_annual_reset_can_restore_missing_counters_without_resetting_battery_stock():
    """A true reset establishes zero new-period usage. An ordinary mid-year quarter cannot do so."""
    assert simulated_counters_ready(contract(bonus=True), datetime(2027, 1, 1, tzinfo=UTC), None)
    assert not simulated_counters_ready(contract(bonus=True), START, None)


@pytest.mark.parametrize(
    "adding, expected_kwh, expected_value", [(True, 1, 0.2), (False, -2, -0.4)]
)
def test_actual_inventory_changes_do_not_require_known_reference_stock(
    adding, expected_kwh, expected_value
):
    """Adding/removing physical energy is external inventory, leaving the .30 measured purchase unchanged."""
    from dataclasses import replace

    selected_problem, selected_frame, selected_bindings = problem(), frame(), bindings()
    previous = {
        "end": START.isoformat(),
        "actual_stored": {"battery": 2},
        "capacities": {"battery": 4},
        "inventory": 0.4,
        "curves": {"battery": {"reserve": 0, "usable": 4, "rate": 0.2}},
    }
    if adding:
        selected_problem = replace(
            selected_problem,
            batteries=(
                *selected_problem.batteries,
                replace(selected_problem.batteries[0], device_id="extra"),
            ),
            terminal_valuations=(
                *selected_problem.terminal_valuations,
                replace(selected_problem.terminal_valuations[0], device_id="extra"),
            ),
        )
        selected_frame = replace(
            selected_frame,
            battery_start={"battery": 2, "extra": 1},
            battery_end={"battery": 2, "extra": 1},
            channels={**selected_frame.channels, "sensor.extra_discharge": 0},
        )
        selected_bindings["batteries"].append(
            {"device_id": "extra", "energy_entity": "sensor.extra_discharge"}
        )
    else:
        selected_problem = replace(selected_problem, batteries=(), terminal_valuations=())
        selected_frame = replace(
            selected_frame,
            battery_start={},
            battery_end={},
            channels={
                key: value
                for key, value in selected_frame.channels.items()
                if key != "sensor.discharge"
            },
        )
        selected_bindings["batteries"] = []
        selected_bindings["terms"] = [
            term for term in selected_bindings["terms"] if term["entity_id"] != "sensor.discharge"
        ]
    result, _counters = independent_account(
        selected_problem,
        selected_frame,
        selected_bindings,
        "actual",
        {},
        contract(),
        (),
        prices(),
        None,
        inventory_previous=previous,
    )
    assert result["economic_cost"] == pytest.approx(0.3)
    assert result["inventory_change"] == pytest.approx(0)
    assert result["inventory_movements"][0]["kwh"] == expected_kwh
    assert result["inventory_movements"][0]["value"] == pytest.approx(expected_value)


def test_valuing_movements_does_not_modify_the_resolved_physical_evidence():
    """Financial valuation preserves the original before/after stock used by daily physical checkpoints."""
    from copy import deepcopy

    from open_energy_planner.savings.accounting import value_movements

    movements = [
        {
            "device_id": "battery",
            "scenario": "actual",
            "kwh": 1,
            "before": 2,
            "after": 3,
            "acquisition_cost": 0,
            "reason": "installation_change",
        }
    ]
    original = deepcopy(movements)
    valued, _external = value_movements(problem(), None, movements)
    assert movements == original
    assert valued[0]["value"] == pytest.approx(0.2)
