"""Measured cash resumes after gaps without battery stock, while missing measurements remain excluded."""

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
def test_measured_cash_recovers_after_a_missing_quarter_without_stock_replay(settlement, stateful):
    """Two recorded 1-kWh quarters cost .60. The middle gap never receives coverage or invented stock/counters."""
    runtime, store, _documents, engine, _plans = settlement
    no_battery = stateful != "battery"
    with engine.begin() as connection:
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
            battery_start={},
            battery_end={},
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
    assert report["scenarios"]["actual"]["coverage_seconds"] == 1800
    assert report["cash"]["actual_cost"] == pytest.approx(0.60)
    assert report["cash"]["without_battery_cost"] == pytest.approx(0.60)
    assert report["cash"]["savings"] == 0
    assert report["cash"]["coverage_seconds"] == 1800
    assert any(
        item["start"] == END.isoformat()
        and item["end"] == (END + timedelta(minutes=15)).isoformat()
        for item in report["scenarios"]["without_battery"]["missing"]
    )
    assert set(report["checkpoint"]) == {"end", "cash_counters"}


def test_absent_solar_meter_preserves_cash_without_inventing_measured_zero(settlement):
    """No installed PV requires no solar channel. Measured solar remains unknown while the matched grid cost is .30."""
    runtime, store, _documents, engine, _plans = settlement
    with engine.begin() as connection:
        connection.execute(delete(SETTLEMENT_FRAMES))
    selected = bindings()
    selected["solar"] = []
    selected["terms"] = [term for term in selected["terms"] if term["entity_id"] != "sensor.pv"]
    measured = replace(
        frame(),
        channels={key: value for key, value in frame().channels.items() if key != "sensor.pv"},
    )
    store.put_frames((measured,), {"bindings": selected, "seeds": [], "location": None})
    report = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", contract(), None, END + timedelta(days=1), START, None
    )
    assert report["energy"]["solar_ac"] == {"kwh": None, "coverage_seconds": 0}
    assert report["scenarios"]["actual"]["totals"]["purchase"] == pytest.approx(0.3)
    assert report["cash"]["coverage_seconds"] == 900
    assert report["cash"]["actual_cost"] == pytest.approx(0.3)
    assert report["cash"]["without_battery_cost"] == pytest.approx(0.3)
    assert report["cash"]["savings"] == 0
