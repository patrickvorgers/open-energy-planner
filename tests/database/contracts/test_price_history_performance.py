"""Repair price indexes safely and preserve small-data query correctness."""

import pytest
from sqlalchemy import Column, Index, MetaData, String, Table, event, inspect, select
from sqlalchemy.exc import OperationalError

from open_energy_planner.prices.storage.adapters.selection_store import SqlAlchemySelectionStore
from open_energy_planner.storage.adapters.lifecycle.schema import (
    DECISIONS,
    PAIRS,
    PRICE_PAIR_TARGETS,
)
from tests.database.support.history_datasets import retained_decisions, retained_pairs


@pytest.mark.parametrize("identified", [False, True])
def test_compact_price_history_keeps_latest_buckets_and_excludes_old_errors(
    selection_store, identified
):
    """Sparse old extremes cannot affect recent MAEs or hide a bucket's independent latest day."""
    from datetime import timedelta

    from open_energy_planner.prices.selection import ForecastSelection
    from open_energy_planner.storage.adapters.lifecycle.schema import ACTUALS
    from tests.database.support.history_datasets import END, WINDOW_START

    adapter, area = selection_store
    if identified:
        adapter = SqlAlchemySelectionStore(adapter.engine, sources=("primary", "alternative"))
    adapter.load(area)
    targets = [WINDOW_START - timedelta(hours=1), END - timedelta(days=2), END - timedelta(days=1)]
    pairs = []
    actuals = []
    for target in targets:
        actuals.append(
            {"area": area, "target": target.isoformat(), "price": 0.0, "version": "c" * 64}
        )
        for bucket in range(3):
            for cohort in range(2):
                pairs.append(
                    {
                        "area": area,
                        "bucket": bucket,
                        "cohort": (target - timedelta(hours=6 + 24 * bucket + cohort)).isoformat(),
                        "target": target.isoformat(),
                        "primary_price": (0.01 if cohort == 0 else 0.03)
                        if target >= WINDOW_START
                        else 999.0,
                        "alternative_price": (-0.005 if cohort == 0 else -0.015)
                        if target >= WINDOW_START
                        else 888.0,
                        "primary_version": "a" * 64,
                        "alternative_version": "b" * 64,
                        **adapter.identities,
                    }
                )
    decisions = [
        {
            **next(retained_decisions(area)),
            "bucket": bucket,
            "day": (END - timedelta(days=bucket + 1)).date().isoformat(),
            **adapter.identities,
        }
        for bucket in range(3)
    ]
    with adapter.engine.begin() as connection:
        connection.execute(adapter.pairs.insert(), pairs)
        connection.execute(ACTUALS.insert(), actuals)
        connection.execute(adapter.decisions.insert(), decisions)
    assert {bucket: row["assessed_on"] for bucket, row in adapter.load(area).items()} == {
        0: "2026-10-21",
        1: "2026-10-20",
        2: "2026-10-19",
    }
    selector = ForecastSelection(adapter, area, "primary", "alternative")
    scored = adapter.update(area, (), (), END, WINDOW_START, selector.decide)
    for bucket in range(3):
        assert scored[bucket]["primary_mae"] == pytest.approx(0.02)
        assert scored[bucket]["alternative_mae"] == pytest.approx(0.01)
        assert scored[bucket]["matched_hours"] == 2
        assert scored[bucket]["settled_days"] == 2
    assert (
        SqlAlchemySelectionStore(
            adapter.engine, sources=("primary", "alternative") if identified else ()
        ).load(area)
        == scored
    )


@pytest.mark.parametrize("fail_rebuild", [False, True])
def test_existing_scoring_index_is_repaired_without_changing_evidence(
    selection_store, fail_rebuild
):
    """An old index is repaired without receipt loss. A failed rebuild reports a fault and a later reopen retries safely."""
    adapter, area = selection_store
    adapter.load(area)
    with adapter.engine.begin() as connection:
        connection.execute(DECISIONS.insert(), next(retained_decisions(area)))
        original_pair = next(retained_pairs(area))
        connection.execute(PAIRS.insert(), original_pair)
    PRICE_PAIR_TARGETS.drop(adapter.engine)
    legacy_table = Table(PAIRS.name, MetaData(), Column("area", String), Column("target", String))
    old_index = Index(PRICE_PAIR_TARGETS.name, legacy_table.c.area, legacy_table.c.target)
    old_index.create(adapter.engine)
    reopened = SqlAlchemySelectionStore(adapter.engine)
    if fail_rebuild:

        def fail_create(_connection, _cursor, statement, _parameters, _context, _many):
            """Interrupt index creation after the old index was dropped, leaving evidence available for retry."""
            if statement.lstrip().upper().startswith("CREATE INDEX"):
                raise OperationalError("fixture private connection details", {}, None)

        event.listen(adapter.engine, "before_cursor_execute", fail_create)
        try:
            with pytest.raises(OSError, match="^forecast_selection_storage_unavailable$"):
                reopened.load(area)
        finally:
            event.remove(adapter.engine, "before_cursor_execute", fail_create)
        assert not reopened.initialized
    assert reopened.load(area)[0]["assessed_on"] == "2021-10-22"
    indexes = inspect(adapter.engine).get_indexes(PAIRS.name)
    assert next(index for index in indexes if index["name"] == PRICE_PAIR_TARGETS.name)[
        "column_names"
    ] == [
        "area",
        "bucket",
        "target",
    ]
    assert SqlAlchemySelectionStore(adapter.engine).load(area) == reopened.load(area)
    with adapter.engine.connect() as connection:
        assert (
            dict(connection.execute(select(PAIRS).where(PAIRS.c.area == area)).mappings().one())
            == original_pair
        )
