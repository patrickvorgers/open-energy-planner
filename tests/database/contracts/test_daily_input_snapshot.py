"""Measured cash rereads published corrections without replaying historical battery dispatch."""

from datetime import timedelta

import pytest

from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
from open_energy_planner.savings.daily import DailySavings
from tests.app.savings.support.scenarios import END, START, contract


def test_next_day_calculation_uses_a_new_published_price_receipt(settlement):
    """Two imported kWh at .10 cost .20. Correcting the first quarter to .80 makes the next calculation .90."""
    runtime, _store, _documents, engine, plans = settlement
    original = plans.get_plan("original-plan")
    daily = DailySavings(runtime)
    first = daily.calculate(
        "2026-10-22", "UTC", contract(buy="market"), END + timedelta(days=1), START
    )
    with engine.begin() as connection:
        retain(
            connection,
            (
                PriceSeries(
                    "synthetic-correction",
                    "NL",
                    "EUR",
                    END + timedelta(hours=1),
                    None,
                    None,
                    (PricePoint(START, END, 0.8, PriceKind.PUBLISHED, 15),),
                ),
            ),
        )
    corrected = daily.calculate(
        "2026-10-22", "UTC", contract(buy="market"), END + timedelta(days=2), START
    )
    assert first["cash"]["actual_cost"] == pytest.approx(0.20)
    assert corrected["cash"]["actual_cost"] == pytest.approx(0.90)
    assert corrected["cash"]["savings"] == 0
    assert first["published_price_versions"] != corrected["published_price_versions"]
    assert plans.get_plan("original-plan") == original
