"""Known scenario export usage survives a pending historical baseline without inventing unknown openings."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
from open_energy_planner.core.calendar import day_window
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
from open_energy_planner.savings.daily import DailySavings
from open_energy_planner.savings.storage.adapters.sqlalchemy import stamp
from open_energy_planner.storage.adapters.lifecycle.retention import thin
from open_energy_planner.storage.adapters.lifecycle.schema import (
    ACTIVATIONS,
    PUBLISHED_PRICES,
    SETTLEMENT_FRAMES,
)
from tests.app.savings.support.scenarios import START, bindings, contract, frame


def export_day(settlement, *, fault=None):
    """Seed two measured export days. Opening actual usage is 1.5 kWh, no-battery .5 kWh, and reference usage is unknown."""
    runtime, store, docs, engine, plans = settlement
    start, end = day_window("2026-10-22", "UTC")
    initial = start - timedelta(days=1)
    selected = contract(bonus=True)
    counter = "bonus|" + (START - timedelta(days=1)).isoformat()
    previous = {
        "end": start.isoformat(),
        "reference": None,
        "reference_known": False,
        "physical": None,
        "physical_known": False,
        "actual": None,
        "actual_counters": {counter: 1.5},
        "without_battery_counters": {counter: 0.5},
        "reference_counters": None,
    }
    plan = deepcopy(plans.get_plan("original-plan"))
    plan.update(plan_id="export-continuity", generated_at=initial.isoformat())
    interval = plan["input_snapshot"]["problem"]["intervals"][0]
    plan["input_snapshot"]["problem"]["intervals"] = [
        {
            **deepcopy(interval),
            "start": (start + timedelta(minutes=index * 15)).isoformat(),
            "load_kwh": 0,
        }
        for index in range(192)
    ]
    plans.state["plan"] = plan
    plans.save()
    with engine.begin() as connection:
        connection.execute(
            ACTIVATIONS.update()
            .where(ACTIVATIONS.c.activated_at == stamp(START))
            .values(plan_id=plan["plan_id"])
        )
        connection.execute(
            ACTIVATIONS.insert().values(activated_at=stamp(start), plan_id=plan["plan_id"])
        )
        connection.execute(SETTLEMENT_FRAMES.delete())
        retain(
            connection,
            (
                PriceSeries(
                    "synthetic-two-days",
                    "NL",
                    "EUR",
                    initial,
                    None,
                    None,
                    tuple(
                        PricePoint(
                            start + timedelta(minutes=index * 15),
                            start + timedelta(minutes=(index + 1) * 15),
                            0.1,
                            PriceKind.PUBLISHED,
                            15,
                        )
                        for index in range(192)
                    ),
                ),
            ),
        )
    context = {"bindings": bindings(), "seeds": [], "location": None}
    rows = tuple(
        replace(
            frame(
                start=start + timedelta(minutes=index * 15),
                imported_kwh=0,
                exported_kwh=0.5,
                pv_kwh=0.5,
            ),
            meter_start={"sensor.export": 10 + index * 0.5},
        )
        for index in range(96)
        if fault != "physical_gap" or index != 48
    )
    store.put_frames(rows, context)
    return (
        runtime,
        store,
        docs,
        engine,
        plans,
        selected,
        counter,
        previous,
        context,
        initial,
        start,
        end,
    )


def test_later_malformed_price_preserves_earlier_independent_bonus_usage(settlement):
    """95 billable quarters retain 47.5 kWh export and .05/.15 bonuses. All 48 measured kWh advance usage."""
    (
        runtime,
        _store,
        _docs,
        engine,
        _plans,
        selected,
        counter,
        previous,
        _context,
        initial,
        start,
        end,
    ) = export_day(settlement)
    with engine.begin() as connection:
        connection.execute(
            PUBLISHED_PRICES.update()
            .where(PUBLISHED_PRICES.c.start == stamp(start + timedelta(hours=12)))
            .values(start="2026-10-22T12:00:PRIVATE")
        )
    report = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", selected, None, end + timedelta(days=4), initial, previous
    )
    for kind, expected_bonus, expected_cost in (
        ("actual", 0.05, 4.70),
        ("without_battery", 0.15, 4.60),
    ):
        scenario = report["scenarios"][kind]
        assert scenario["coverage_seconds"] == 85500
        assert scenario["totals"]["export_kwh"] == 47.5
        assert scenario["totals"]["bonus"] == pytest.approx(expected_bonus)
        assert scenario["totals"]["economic_cost"] == pytest.approx(expected_cost)
    assert report["scenarios"]["reference"]["totals"] is None
    assert report["checkpoint"]["actual_counters"] == {counter: 49.5}
    assert report["checkpoint"]["without_battery_counters"] == {counter: 48.5}
    assert report["energy"]["grid_export"] == {"kwh": 48, "coverage_seconds": 86400}
    assert "PRIVATE" not in str(report) and "PRIVATE" not in runtime.issue


def test_pending_baseline_preserves_known_actual_billing_through_seal_and_expiry(settlement):
    """A 2-kWh cap pays .05 actual/.15 no-battery on distinct openings. Costs are 4.75/4.65 then 4.80 each, without borrowing."""
    (
        runtime,
        store,
        docs,
        _engine,
        plans,
        selected,
        counter,
        previous,
        context,
        initial,
        _start,
        end,
    ) = export_day(settlement)
    now = end + timedelta(days=4)
    daily = DailySavings(runtime)
    report = daily.calculate("2026-10-22", "UTC", selected, None, now, initial, previous)
    assert runtime.bonus.pending is not None
    assert runtime.bonus.pending.periods == (("bonus", START - timedelta(days=1)),)
    assert report["scenarios"]["actual"]["coverage_seconds"] == 86400
    assert report["scenarios"]["actual"]["totals"]["bonus"] == pytest.approx(0.05)
    assert report["scenarios"]["actual"]["totals"]["economic_cost"] == pytest.approx(4.75)
    assert report["scenarios"]["without_battery"]["coverage_seconds"] == 86400
    assert report["scenarios"]["without_battery"]["totals"]["bonus"] == pytest.approx(0.15)
    assert report["scenarios"]["without_battery"]["totals"]["economic_cost"] == pytest.approx(4.65)
    assert report["checkpoint"]["actual_counters"] == {counter: 49.5}
    assert report["checkpoint"]["without_battery_counters"] == {counter: 48.5}
    assert report["checkpoint"]["reference_counters"] is None
    assert report["scenarios"]["reference"]["status"] == "unavailable"
    assert report["savings"] is None
    report["finalized_at"] = now.isoformat()
    store.put_day(report, docs, "2026-10-23")
    sealed = store.day(finalized=True)
    thin(plans, docs, now, "UTC")
    assert store.frames() == []
    later_rows = tuple(
        replace(
            frame(
                start=end + timedelta(minutes=index * 15),
                imported_kwh=0,
                exported_kwh=0.5,
                pv_kwh=0.5,
            ),
            meter_start={"sensor.export": 58 + index * 0.5},
        )
        for index in range(96)
    )
    store.put_frames(later_rows, context)
    seed = CounterSeed("bonus", START - timedelta(days=1), 49.5, 58, "sensor.export", end)
    following = daily.calculate(
        "2026-10-23", "UTC", selected, CounterBook(end, (seed,)), now, initial, sealed["checkpoint"]
    )
    assert following["scenarios"]["actual"]["totals"]["economic_cost"] == pytest.approx(4.8)
    assert following["scenarios"]["without_battery"]["totals"]["economic_cost"] == pytest.approx(
        4.8
    )
    assert following["checkpoint"]["actual_counters"] == {counter: 97.5}
    assert following["checkpoint"]["without_battery_counters"] == {counter: 96.5}
    assert following["checkpoint"]["reference_counters"] is None
    assert following["savings"] is None
    assert store.day(finalized=True) == sealed


def test_physical_gap_invalidates_known_export_continuation(settlement):
    """Half a measured day costs 2.35. A missing quarter makes later usage unknown, never a new allowance."""
    (
        runtime,
        _store,
        _docs,
        _engine,
        _plans,
        selected,
        _counter,
        previous,
        _context,
        initial,
        _start,
        end,
    ) = export_day(settlement, fault="physical_gap")
    report = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", selected, None, end + timedelta(days=4), initial, previous
    )
    assert report["scenarios"]["actual"]["coverage_seconds"] == 43200
    assert report["scenarios"]["actual"]["totals"]["economic_cost"] == pytest.approx(2.35)
    assert report["scenarios"]["without_battery"]["totals"]["economic_cost"] == pytest.approx(2.25)
    assert report["checkpoint"]["actual_counters"] is None
    assert report["checkpoint"]["without_battery_counters"] is None
    assert report["checkpoint"]["reference_counters"] is None
    assert report["savings"] is None


@pytest.mark.parametrize("seed_kind", ["valid_actual", "changed_source", "reset"])
def test_unknown_simulated_openings_cannot_borrow_a_later_actual_confirmation(
    settlement, seed_kind
):
    """A sealed unknown day stays unknown. Only a valid actual seed can bill the next day. Simulated usage is never borrowed."""
    (
        runtime,
        store,
        docs,
        _engine,
        plans,
        selected,
        counter,
        previous,
        context,
        initial,
        _start,
        end,
    ) = export_day(settlement)
    previous.update(actual_counters=None, without_battery_counters=None)
    daily = DailySavings(runtime)
    now = end + timedelta(days=4)
    report = daily.calculate("2026-10-22", "UTC", selected, None, now, initial, previous)
    assert all(value["status"] == "unavailable" for value in report["scenarios"].values())
    assert report["checkpoint"]["actual_counters"] is None
    report["finalized_at"] = now.isoformat()
    store.put_day(report, docs, "2026-10-23")
    sealed = store.day(finalized=True)
    thin(plans, docs, now, "UTC")
    assert store.frames() == []
    later = tuple(
        replace(
            frame(
                start=end + timedelta(minutes=index * 15),
                imported_kwh=0,
                exported_kwh=0.5,
                pv_kwh=0.5,
            ),
            meter_start={"sensor.export": 58 + index * 0.5},
        )
        for index in range(96)
    )
    store.put_frames(later, context)
    seed = CounterSeed(
        "bonus",
        START - timedelta(days=1),
        49.5,
        1000 if seed_kind == "reset" else 58,
        "sensor.other_export" if seed_kind == "changed_source" else "sensor.export",
        end,
    )
    following = daily.calculate(
        "2026-10-23", "UTC", selected, CounterBook(end, (seed,)), now, initial, sealed["checkpoint"]
    )
    if seed_kind == "valid_actual":
        assert following["scenarios"]["actual"]["totals"]["economic_cost"] == pytest.approx(4.8)
        assert following["checkpoint"]["actual_counters"] == {counter: 97.5}
    else:
        assert following["scenarios"]["actual"]["totals"] is None
        assert following["checkpoint"]["actual_counters"] is None
    assert following["checkpoint"]["reference_counters"] is None
    assert following["checkpoint"]["without_battery_counters"] is None
    assert following["scenarios"]["reference"]["totals"] is None
    assert following["scenarios"]["without_battery"]["totals"] is None
    assert following["savings"] is None
    assert store.day(finalized=True) == sealed
