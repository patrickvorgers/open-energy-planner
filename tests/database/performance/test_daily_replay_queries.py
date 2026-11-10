"""Daily calculation reads bounded chronology and prices once per replay on every backend."""

from datetime import timedelta

import pytest
from sqlalchemy import event

from open_energy_planner.core.calendar import day_window
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
from open_energy_planner.savings.daily import DailySavings
from open_energy_planner.savings.storage.adapters.sqlalchemy import stamp
from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS
from tests.app.savings.support.scenarios import bindings, contract, frame


@pytest.mark.parametrize(
    "day,zone,quarters", [("2026-10-22", "UTC", 96), ("2026-10-25", "Europe/Amsterdam", 100)]
)
def test_full_day_replay_query_budget_preserves_independent_amounts(
    settlement, day, zone, quarters, record_testsuite_property
):
    """A full day costs .30 per measured quarter, minus .20 economic reference benefit from its original 2-kWh stock."""
    runtime, store, _docs, engine, _plans = settlement
    start, end = day_window(day, zone)
    with engine.begin() as connection:
        connection.execute(
            ACTIVATIONS.insert().values(activated_at=stamp(start), plan_id="original-plan")
        )
        retain(
            connection,
            (
                PriceSeries(
                    "synthetic-day",
                    "NL",
                    "EUR",
                    start - timedelta(hours=1),
                    None,
                    None,
                    tuple(
                        PricePoint(
                            start + timedelta(minutes=15 * index),
                            start + timedelta(minutes=15 * (index + 1)),
                            0.1,
                            PriceKind.PUBLISHED,
                            15,
                        )
                        for index in range(quarters)
                    ),
                ),
            ),
        )
    store.put_frames(
        tuple(frame(start=start + timedelta(minutes=15 * index)) for index in range(quarters)),
        {"bindings": bindings(), "seeds": [], "location": None},
    )
    statements = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        """Measure real SQL round trips instead of assuming a helper call performs one query."""
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    reports = []
    counts = []
    try:
        for _ in range(2):
            statements.clear()
            report = DailySavings(runtime).calculate(
                day, zone, contract(buy="market + 0.2"), None, end + timedelta(hours=1), start, None
            )
            reports.append(report)
            assert report["scenarios"]["actual"]["totals"]["purchase"] == pytest.approx(
                quarters * 0.3
            )
            assert report["scenarios"]["reference"]["totals"]["economic_cost"] == pytest.approx(
                quarters * 0.3 - 0.2
            )
            assert report["scenarios"]["reference"]["coverage_seconds"] == quarters * 900
            counts.append(len(statements))
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert reports[0] == reports[1]
    record_testsuite_property(f"{engine.dialect.name}:{day}:replay-selects", str(counts))
    assert max(counts) <= 12, f"Day replays issued {counts} SELECT statements"
    with engine.begin() as connection:
        retain(
            connection,
            (
                PriceSeries(
                    "synthetic-correction",
                    "NL",
                    "EUR",
                    end + timedelta(hours=1),
                    None,
                    None,
                    (
                        PricePoint(
                            start, start + timedelta(minutes=15), 0.4, PriceKind.PUBLISHED, 15
                        ),
                    ),
                ),
            ),
        )
    corrected = DailySavings(runtime).calculate(
        day, zone, contract(buy="market + 0.2"), None, end + timedelta(hours=2), start, None
    )
    assert corrected["scenarios"]["actual"]["totals"]["purchase"] == pytest.approx(
        quarters * 0.3 + 0.3
    )
    assert corrected["savings"] == pytest.approx(-0.5)
    assert corrected["published_price_versions"] != reports[0]["published_price_versions"]
