"""Quota-free daily estimates need no historical export allowance."""

from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.core.calendar import day_window
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
from open_energy_planner.savings.daily import DailySavings
from open_energy_planner.storage.adapters.lifecycle.schema import SETTLEMENT_FRAMES
from tests.app.savings.support.scenarios import START, bindings, contract, frame


def test_savings_prices_all_available_quarters_without_bonus_history(settlement):
    """Price two days independently, including nine hours with hypothetical export above the cap."""
    runtime, store, documents, engine, _plans = settlement
    date = START.date()
    start, _ = day_window(date.isoformat(), "UTC")
    day_count = 2
    end = start + timedelta(days=day_count)
    quarter = timedelta(minutes=15)
    readings = tuple(start + index * quarter for index in range(192))
    export_indices = set(range(32, 49)) | set(range(128, 147))
    context_bindings = bindings()
    context_bindings["terms"].append({"entity_id": "sensor.charge", "sign": -1})
    rows = []
    for index, instant in enumerate(readings):
        exporting = index in export_indices
        measurement = (
            frame(start=instant, pv_kwh=3)
            if exporting
            else frame(start=instant, discharged_kwh=0.5)
        )
        rows.append(
            replace(
                measurement,
                channels={**measurement.channels, "sensor.charge": 2 if exporting else 0},
            )
        )
    with engine.begin() as connection:
        connection.execute(SETTLEMENT_FRAMES.delete())
        retain(
            connection,
            (
                PriceSeries(
                    "two-day-bonus-coverage",
                    "NL",
                    "EUR",
                    START + timedelta(hours=1),
                    None,
                    None,
                    tuple(
                        PricePoint(instant, instant + quarter, 0.1, PriceKind.PUBLISHED, 15)
                        for instant in readings
                    ),
                ),
            ),
        )
    store.put_frames(
        rows,
        {
            "bindings": context_bindings,
            "seeds": [],
            "location": None,
            "market_identity": {"currency": "EUR", "market_area": "NL"},
        },
    )
    daily = DailySavings(runtime)
    selected = contract(bonus=True)
    assert selected.bonuses[0].cap_kwh == 2
    for offset in range(day_count):
        day = date + timedelta(days=offset)
        report = daily.calculate(
            day.isoformat(), "UTC", selected, end + timedelta(hours=12), start - timedelta(days=1)
        )
        assert report["cash"]["coverage_seconds"] == 86400
        assert not any(scenario["missing"] for scenario in report["scenarios"].values())
        assert set(report["checkpoint"]) == {"end"}
        store.put_day(report, documents, (day + timedelta(days=1)).isoformat())
    assert runtime.bonus.pending is None
    assert runtime.issue is None

    cash = daily.overview(start, end)["cash"]
    assert cash["bonus_volume_cap_ignored"] is True
    assert cash["coverage_seconds"] == 48 * 3600
    assert cash["breakdown"]["actual"] == {"purchase": pytest.approx(57.60), "sale": 0, "bonus": 0}
    assert cash["breakdown"]["without_battery"] == {
        "purchase": pytest.approx(70.20),
        "sale": pytest.approx(-3.60),
        "bonus": pytest.approx(3.60),
    }
    assert cash["savings"] == pytest.approx(12.60)
    assert [row["breakdown"]["without_battery"]["bonus"] for row in cash["chart"]] == pytest.approx(
        [1.70, 1.90]
    )

    next_day = end.date().isoformat()
    empty = daily.calculate(next_day, "UTC", selected, end + timedelta(days=2), start)
    store.put_day(empty, documents, (end + timedelta(days=1)).date().isoformat())
    with_gap = daily.overview(start, end + timedelta(days=1))["cash"]
    assert with_gap["coverage_seconds"] == cash["coverage_seconds"]
    assert with_gap["breakdown"] == cash["breakdown"]
    assert with_gap["chart"][-1]["savings"] is None
