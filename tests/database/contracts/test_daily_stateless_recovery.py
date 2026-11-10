"""Only a reference with no battery stock or allowance state can resume independently after a gap."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import delete

from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
from open_energy_planner.savings.daily import DailySavings
from open_energy_planner.storage.adapters.lifecycle.schema import SETTLEMENT_FRAMES
from tests.app.savings.support.scenarios import END, START, bindings, contract, frame


@pytest.mark.parametrize("stateful", [None, "battery", "bonus"])
def test_only_stateless_reference_recovers_after_a_missing_quarter(settlement, stateful):
    """Two recorded 1-kWh quarters cost .60. The middle gap never receives coverage or invented stock/counters."""
    runtime, store, _documents, engine, plans = settlement
    plan = deepcopy(plans.get_plan("original-plan"))
    no_battery = stateful != "battery"
    if no_battery:
        plan["input_snapshot"]["problem"]["batteries"] = []
        plan["input_snapshot"]["problem"]["terminal_valuations"] = []
    plan["plan_id"] = "gap-recovery"
    plans.state["plan"] = plan
    plans.save()
    from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS

    with engine.begin() as connection:
        connection.execute(ACTIVATIONS.update().values(plan_id=plan["plan_id"]))
        connection.execute(delete(SETTLEMENT_FRAMES))
        retain(
            connection,
            (
                PriceSeries(
                    "synthetic-gap",
                    "NL",
                    "EUR",
                    START - timedelta(hours=1),
                    None,
                    None,
                    tuple(
                        PricePoint(
                            START + timedelta(minutes=15 * index),
                            START + timedelta(minutes=15 * (index + 1)),
                            0.1,
                            PriceKind.PUBLISHED,
                            15,
                        )
                        for index in range(3)
                    ),
                ),
            ),
        )
    selected = bindings()
    if no_battery:
        selected["batteries"] = []
    rows = tuple(
        replace(
            frame(start=START + timedelta(minutes=15 * index)),
            battery_start={} if no_battery else {"battery": 2},
            battery_end={} if no_battery else {"battery": 2},
        )
        for index in (0, 2)
    )
    store.put_frames(rows, {"bindings": selected, "seeds": [], "location": None})
    report = DailySavings(runtime).calculate(
        "2026-10-22",
        "UTC",
        contract(bonus=stateful == "bonus"),
        CounterBook(
            START, (CounterSeed("bonus", START - timedelta(days=1), 0, 10, "sensor.export", START),)
        )
        if stateful == "bonus"
        else None,
        END + timedelta(days=1),
        START,
        None,
    )
    reference = report["scenarios"]["reference"]
    assert report["scenarios"]["actual"]["coverage_seconds"] == 1800
    assert report["scenarios"]["actual"]["totals"]["purchase"] == pytest.approx(0.60)
    assert reference["coverage_seconds"] == (1800 if stateful is None else 900)
    if stateful is None:
        assert reference["totals"]["purchase"] == pytest.approx(0.60)
        assert report["comparison_seconds"] == 1800
        assert any(
            item["start"] == END.isoformat()
            and item["end"] == (END + timedelta(minutes=15)).isoformat()
            for item in reference["missing"]
        )
        assert reference["closing"]["at"] == (START + timedelta(minutes=45)).isoformat()
        assert reference["closing"]["stored_kwh"] == {}
    else:
        assert not report["checkpoint"]["reference_known"]


def test_absent_solar_meter_does_not_invent_measured_zero_or_break_no_solar_physics(settlement):
    """No installed PV legitimately has zero modeled generation, while its absent meter stays unavailable."""
    runtime, store, _documents, engine, plans = settlement
    plan = deepcopy(plans.get_plan("original-plan"))
    plan["plan_id"] = "no-solar"
    plan["input_snapshot"]["problem"]["pv_sources"] = []
    for interval in plan["input_snapshot"]["problem"]["intervals"]:
        interval["pv_sources"] = []
    plans.state["plan"] = plan
    plans.save()
    from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS

    with engine.begin() as connection:
        connection.execute(ACTIVATIONS.update().values(plan_id=plan["plan_id"]))
        connection.execute(delete(SETTLEMENT_FRAMES))
    selected = bindings()
    selected["solar"] = []
    store.put_frames((frame(),), {"bindings": selected, "seeds": [], "location": None})
    report = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", contract(), None, END + timedelta(days=1), START, None
    )
    assert report["energy"]["solar_ac"] == {"kwh": None, "coverage_seconds": 0}
    assert report["scenarios"]["actual"]["totals"]["purchase"] == pytest.approx(0.3)
    assert report["scenarios"]["reference"]["coverage_seconds"] == 900
