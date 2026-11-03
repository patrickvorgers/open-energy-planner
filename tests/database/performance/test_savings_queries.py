"""Large settlement history must stay aggregated and bounded on every backend."""

from datetime import timedelta

import pytest

from tests.app.savings.support.scenarios import END, START, contract
from tests.database.support.history_datasets import settlement_batches


def test_three_year_period_query_aggregates_without_loading_all_detail(settlement):
    """A full 2023 has 35040 quarters: benefit -3504, while the browser receives at most 96 detail records."""
    from datetime import UTC, datetime
    from time import perf_counter

    from open_energy_planner.storage.adapters.lifecycle.schema import (
        SETTLEMENT_EVALUATIONS,
        SETTLEMENT_FRAMES,
    )

    runtime, store, docs, engine, _plans = settlement
    runtime.process(contract(), END + timedelta(hours=1))
    head = docs.read("head.json")
    sample = store.results(head["revision"], START, END)[0]
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
