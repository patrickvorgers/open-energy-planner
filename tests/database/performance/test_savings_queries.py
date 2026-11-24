"""Settlement capture and historical queries stay bounded on every backend."""

from datetime import timedelta

import pytest
from sqlalchemy import event

from tests.app.savings.support.scenarios import END, bindings, frame
from tests.database.support.history_datasets import retained_evaluation, settlement_batches


@pytest.mark.parametrize(("quarters", "batches"), [(96, 1), (100, 2)])
def test_capture_uses_bounded_queries_and_retry_does_not_write(settlement, quarters, batches):
    """Full-day writes use one read and insert execution per batch. Identical retries only read."""
    _runtime, store, _docs, engine, _plans = settlement
    frames = tuple(
        frame(start=END + timedelta(minutes=15 * quarter)) for quarter in range(1, quarters + 1)
    )
    context = {"bindings": bindings(), "seeds": [], "location": None}
    statements = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        """Record statement types sent through the real backend cursor."""
        statements.append(statement.split(maxsplit=1)[0].upper())

    event.listen(engine, "before_cursor_execute", count)
    try:
        store.put_frames(frames, context)
        assert statements.count("SELECT") == batches
        assert statements.count("INSERT") == batches

        statements.clear()
        store.put_frames(frames, context)
        assert statements.count("SELECT") == batches
        assert statements.count("INSERT") == 0
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(store.frames(limit=quarters + 2)) == quarters + 2


def test_three_year_period_query_aggregates_without_loading_all_detail(settlement):
    """A full 2023 has 35040 quarters: benefit -3504, while the browser receives at most 96 detail records."""
    from datetime import UTC, datetime
    from time import perf_counter

    from open_energy_planner.storage.adapters.lifecycle.schema import (
        SETTLEMENT_EVALUATIONS,
        SETTLEMENT_FRAMES,
    )

    runtime, _store, docs, engine, _plans = settlement
    head, sample = retained_evaluation(docs)
    first, last = datetime(2022, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)
    count = int((last - first).total_seconds() / 900)
    for frames, evaluations in settlement_batches(sample, head["revision"], first, last):
        with engine.begin() as connection:
            connection.execute(SETTLEMENT_FRAMES.insert(), frames)
            connection.execute(SETTLEMENT_EVALUATIONS.insert(), evaluations)
    begin = perf_counter()
    result = runtime.overview(datetime(2023, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC))
    elapsed = perf_counter() - begin
    assert result["coverage"]["evaluated"] == 35040
    assert result["savings"] == pytest.approx(-3504)
    assert len(result["intervals"]) == 96
    assert result["details_truncated"]
    assert len(result["chart"]) == 365
    full = runtime.overview(first, last)
    assert len(full["chart"]) == 36
    assert full["coverage"]["evaluated"] == count
    assert elapsed < 30  # Detect a read-all regression, not a hardware-specific performance target.
