"""Independent daily accounting preserves independent bonus boundaries when matching cash coverage is unavailable."""

from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.contracts.bonus import CounterSeed
from open_energy_planner.savings.accounting import (
    cash_account,
    comparison_counters_ready,
    measured_cash_flows,
)
from open_energy_planner.savings.model import PendingSettlement
from tests.app.savings.support.scenarios import START, bindings, contract, frame, prices


def test_new_retroactive_family_cannot_borrow_actual_seed_after_daily_closure():
    """An actual .50-kWh used allowance does not establish simulated exports before the recovery floor."""
    selected = contract(bonus=True)
    seed = CounterSeed("bonus", START - timedelta(days=1), 0.5, 10, "sensor.export", START)
    with pytest.raises(PendingSettlement, match="Without-battery bonus usage is unknown"):
        cash_account(
            selected,
            frame(imported_kwh=0, exported_kwh=0.5, pv_kwh=0.5),
            measured_cash_flows(frame(imported_kwh=0, exported_kwh=0.5, pv_kwh=0.5), bindings())[
                "without_battery"
            ],
            {},
            (seed,),
            prices(),
            None,
            actual=False,
        )
    result, _counters = cash_account(
        selected,
        frame(),
        measured_cash_flows(frame(), bindings())["actual"],
        {},
        (seed,),
        prices(),
        None,
        actual=True,
    )
    assert result["purchase"] == pytest.approx(0.3)


@pytest.mark.parametrize("kind", ["actual", "without_battery"])
def test_zero_export_cost_does_not_require_or_create_a_bonus_opening(kind):
    """A .30 import costs .30 for every possible remaining allowance, while usage stays unknown."""
    result, counters = cash_account(
        contract(bonus=True),
        frame(),
        measured_cash_flows(frame(), bindings())[kind],
        None,
        (),
        prices(),
        None,
        actual=kind == "actual",
    )
    assert result["grid_cash"] == pytest.approx(0.3)
    assert result["bonus"] == 0
    assert counters is None


def test_zero_rate_export_remains_billable_without_inventing_gross_usage():
    """A zero-rate rule pays zero on .5 exported kWh, even when its remaining quota is unknown."""
    from dataclasses import replace

    from open_energy_planner.contracts.calculation.formula import Formula

    selected = contract(bonus=True)
    selected = replace(selected, bonuses=(replace(selected.bonuses[0], rate_formula=Formula("0")),))
    result, counters = cash_account(
        selected,
        frame(imported_kwh=0, exported_kwh=0.5, pv_kwh=0.5),
        measured_cash_flows(frame(imported_kwh=0, exported_kwh=0.5, pv_kwh=0.5), bindings())[
            "without_battery"
        ],
        None,
        (),
        prices(),
        None,
        actual=False,
    )
    assert result["grid_cash"] == pytest.approx(0.05)
    assert result["bonus"] == 0
    assert counters is None


@pytest.mark.parametrize("zero_rate", [False, True])
@pytest.mark.parametrize("seed_failure", ["reset", "future"])
def test_unusable_seed_cannot_hide_an_exact_zero_payment_or_create_usage(zero_rate, seed_failure):
    """A reset/future opening stays unknown. Zero export costs .30, zero-rate .5-kWh export costs .05."""
    from dataclasses import replace

    from open_energy_planner.contracts.calculation.formula import Formula

    selected = contract(bonus=True)
    selected_frame = frame()
    if zero_rate:
        selected = replace(
            selected, bonuses=(replace(selected.bonuses[0], rate_formula=Formula("0")),)
        )
        selected_frame = frame(imported_kwh=0, exported_kwh=0.5, pv_kwh=0.5)
    seed = CounterSeed(
        "bonus",
        START - timedelta(days=1),
        0.5,
        1000 if seed_failure == "reset" else 10,
        "sensor.export",
        START + timedelta(minutes=15) if seed_failure == "future" else START,
    )
    result, counters = cash_account(
        selected,
        selected_frame,
        measured_cash_flows(selected_frame, bindings())["actual"],
        None,
        (seed,),
        prices(),
        None,
        actual=True,
    )
    assert result["grid_cash"] == pytest.approx(0.05 if zero_rate else 0.3)
    assert result["bonus"] == 0
    assert counters is None
    with pytest.raises(ValueError, match="reset|initial comparison budget"):
        cash_account(
            contract(bonus=True),
            frame(imported_kwh=0, exported_kwh=0.5, pv_kwh=0.5),
            measured_cash_flows(frame(imported_kwh=0, exported_kwh=0.5, pv_kwh=0.5), bindings())[
                "actual"
            ],
            None,
            (seed,),
            prices(),
            None,
            actual=True,
        )


def test_original_common_start_may_share_actual_opening_allowance_once():
    """Before scenarios diverge, the same established .50-kWh opening usage is valid for both."""
    selected = contract(bonus=True)
    seed = CounterSeed("bonus", START - timedelta(days=1), 0.5, 10, "sensor.export", START)
    _result, counters = cash_account(
        selected,
        frame(),
        measured_cash_flows(frame(), bindings())["without_battery"],
        {},
        (seed,),
        prices(),
        None,
        common_start=True,
        actual=False,
    )
    assert list(counters.values()) == [0.5]


def test_annual_reset_can_restore_missing_counters_without_resetting_battery_stock():
    """A true reset establishes zero new-period usage. An ordinary mid-year quarter cannot do so."""
    assert comparison_counters_ready(contract(bonus=True), datetime(2027, 1, 1, tzinfo=UTC), None)
    assert not comparison_counters_ready(contract(bonus=True), START, None)
