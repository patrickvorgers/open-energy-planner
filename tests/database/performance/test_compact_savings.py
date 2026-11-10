"""Long-lived compact results and shared graph collection must stay bounded on every backend."""

from datetime import UTC, datetime, timedelta
from time import perf_counter

import pytest
from sqlalchemy import func, select

from open_energy_planner.savings.daily import DailySavings
from open_energy_planner.savings.storage.adapters.sqlalchemy import stamp
from open_energy_planner.storage.adapters.lifecycle.retention import collect_blocks
from open_energy_planner.storage.adapters.lifecycle.schema import (
    PARTS,
    PLANS,
    RECORDS,
    SAVINGS_DAYS,
    VALUES,
)
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import canonical, digest


def test_eight_year_daily_query_aggregates_without_loading_all_reports(settlement):
    """A 365-day year costs 219 actual and 146 self-consumption, with -73 benefit. Eight years return 366 details and 96 monthly bins."""
    runtime, _store, _docs, engine, _plans = settlement
    first, end = datetime(2018, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)
    totals = {}
    for kind, cost, imported, exported in (
        ("actual", 0.6, 2, 2 / 3),
        ("reference", 0.4, 4 / 3, 0),
        ("without_battery", 0.6, 2, 2 / 3),
    ):
        totals[kind] = {
            "purchase": cost,
            "sale": 0,
            "bonus": 0,
            "grid_cash": cost,
            "wear": 0,
            "inventory_change": 0,
            "economic_cost": cost,
            "import_kwh": imported,
            "export_kwh": exported,
        }
    # Each synthetic day discharges/refills 2/3 reference kWh. Actual storage is idle,
    # exports receive zero payment, and both battery inventories finish unchanged.
    batch = []
    for index in range((end - first).days):
        start = first + timedelta(days=index)
        following = start + timedelta(days=1)
        report = {
            "day": start.date().isoformat(),
            "timezone": "UTC",
            "start": start.isoformat(),
            "end": following.isoformat(),
            "currency": "EUR",
            "market_area": "NL",
            "expected_seconds": 86400,
            "finalized_at": (following + timedelta(days=3)).isoformat(),
            "policy": "daily-savings-v1",
            "scenarios": {
                kind: {
                    "totals": values,
                    "coverage_seconds": 86400,
                    "estimated_seconds": 0,
                    "status": "complete",
                    "missing": [],
                }
                for kind, values in totals.items()
            },
            "savings": -0.2,
            "battery_savings": 0,
            "self_consumption_savings": 0.2,
            "checkpoint": {},
        }
        batch.append(
            {
                "start": stamp(start),
                "end": stamp(following),
                "day": report["day"],
                "timezone": "UTC",
                "currency": "EUR",
                "market_area": "NL",
                "finalized_at": report["finalized_at"],
                "payload": canonical(report),
                "payload_hash": digest(report),
                **{
                    f"{kind}_{key}": value
                    for kind, values in totals.items()
                    for key, value in values.items()
                },
                **{f"{kind}_coverage_seconds": 86400 for kind in totals},
                "savings": -0.2,
                "avoided_import_kwh": -2 / 3,
                "comparison_seconds": 86400,
                "battery_savings": 0,
                "battery_comparison_seconds": 86400,
                "self_consumption_savings": 0.2,
                "self_consumption_comparison_seconds": 86400,
            }
        )
        if len(batch) == 128 or following == end:
            with engine.begin() as connection:
                connection.execute(SAVINGS_DAYS.insert(), batch)
            batch.clear()
    began = perf_counter()
    daily = DailySavings(runtime)
    year = daily.overview(datetime(2023, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC))
    full = daily.overview(first, end)
    elapsed = perf_counter() - began
    assert year["day_count"] == 365
    assert year["totals"]["actual"]["economic_cost"] == pytest.approx(219)
    assert year["totals"]["reference"]["economic_cost"] == pytest.approx(146)
    assert year["savings"] == pytest.approx(-73)
    assert len(year["days"]) == 365
    assert len(full["days"]) == 366
    assert len(full["chart"]) == 96
    assert full["day_count"] == 2922
    assert full["details_truncated"] is True
    assert elapsed < 30  # Detect an unbounded read regression, not a hardware benchmark.


def test_graph_collection_preserves_fifty_thousand_referenced_leaves(settlement):
    """A 50,000-leaf graph keeps every referenced value and removes only 128 orphan leaves without quadratic scans."""
    _runtime, _store, _docs, engine, _plans = settlement
    document = {f"synthetic_field_{index}": index for index in range(50000)}
    document.update(
        plan_id="large-retained-graph",
        generated_at="2026-10-01T00:00:00+00:00",
        strategy="minimize cost",
        solution_status="optimal",
    )
    root = digest(document)
    values = {digest(value): canonical(value) for value in document.values()}
    orphans = {
        digest(f"expired synthetic value {index}"): canonical(f"expired synthetic value {index}")
        for index in range(128)
    }
    with engine.begin() as connection:
        known = set(connection.scalars(select(VALUES.c.digest)))
        rows = [
            {"digest": identity, "payload": payload}
            for identity, payload in {**values, **orphans}.items()
            if identity not in known
        ]
        for offset in range(0, len(rows), 256):
            connection.execute(VALUES.insert(), rows[offset : offset + 256])
        connection.execute(RECORDS.insert().values(digest=root))
        parts = [
            {"owner": root, "field": field, "value_ref": digest(value), "record_ref": None}
            for field, value in document.items()
        ]
        for offset in range(0, len(parts), 256):
            connection.execute(PARTS.insert(), parts[offset : offset + 256])
        connection.execute(
            PLANS.insert().values(
                plan_id=document["plan_id"],
                record_ref=root,
                generated_at="2026-10-01T00:00:00.000000+00:00",
                strategy="minimize cost",
                solution_status="optimal",
            )
        )
    began = perf_counter()
    with engine.begin() as connection:
        assert collect_blocks(connection) == 128
        assert (
            connection.scalar(select(func.count()).select_from(PARTS).where(PARTS.c.owner == root))
            == 50004
        )
        assert (
            connection.scalar(
                select(func.count()).select_from(VALUES).where(VALUES.c.digest.in_(orphans))
            )
            == 0
        )
        assert (
            connection.scalar(select(VALUES.c.digest).where(VALUES.c.digest == digest(49999)))
            is not None
        )
    assert perf_counter() - began < 30
