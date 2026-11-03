"""Five retained years must not enlarge latest-state reads or the thirty-day scoring window."""

import json
from itertools import islice
from pathlib import Path
from statistics import median
from time import perf_counter

import pytest
from sqlalchemy import event, func, select

from open_energy_planner.prices.selection import ForecastSelection
from open_energy_planner.prices.storage.adapters.selection_store import SqlAlchemySelectionStore
from open_energy_planner.storage.adapters.lifecycle.schema import (
    ACTUALS,
    DECISIONS,
    PAIRS,
    PRICE_PAIR_TARGETS,
)
from tests.database.support.history_datasets import (
    DATASET_BATCH_ROWS,
    END,
    START,
    WINDOW_START,
    retained_actuals,
    retained_decisions,
    retained_pairs,
)

TIMING_REPEATS = 7


def insert_rows(connection, table, rows):
    """Seed bounded batches directly so the fixture measures reads, not thousands of ingestion transactions."""
    rows = iter(rows)
    while batch := list(islice(rows, DATASET_BATCH_ROWS)):
        connection.execute(table.insert(), batch)


def analyze(connection, tables=(PAIRS, ACTUALS, DECISIONS)):
    """Refresh native optimizer statistics using only known test table names."""
    for table in tables:
        prefix = "ANALYZE TABLE " if connection.dialect.name in {"mysql", "mariadb"} else "ANALYZE "
        connection.exec_driver_sql(prefix + table.name).close()


def explain(connection, statement, parameters):
    """Collect native query plans without hints or dialect details escaping the test boundary."""
    dialect = connection.dialect.name
    prefix = (
        "EXPLAIN QUERY PLAN "
        if dialect == "sqlite"
        else "EXPLAIN (FORMAT JSON) "
        if dialect == "postgresql"
        else "EXPLAIN FORMAT=JSON "
    )
    with connection.exec_driver_sql(prefix + statement, parameters) as result:
        if dialect == "sqlite":
            return [list(row) for row in result]
        plan = result.scalar_one()
        return json.loads(plan) if isinstance(plan, str) else plan


def elapsed(call):
    """Measure warmed query calls without a hardware-dependent pass/fail timing threshold."""
    samples = []
    for _ in range(TIMING_REPEATS):
        start = perf_counter()
        call()
        samples.append((perf_counter() - start) * 1000)
    return {"median_ms": median(samples), "samples_ms": samples}


@pytest.mark.parametrize("identified", [False, True])
def test_five_year_history_uses_bounded_reads_and_recent_scoring(selection_store, identified):
    """Five hourly years retain every row. Three indexed latest reads and a 30-day score match independent arithmetic."""
    adapter, area = selection_store
    if identified:
        adapter = SqlAlchemySelectionStore(adapter.engine, sources=("primary", "alternative"))
    pair_table, decision_table = adapter.pairs, adapter.decisions
    assert adapter.load(area) == {}
    hours = int((END - START).total_seconds() / 3600)
    with adapter.engine.begin() as connection:
        insert_rows(
            connection, pair_table, ({**row, **adapter.identities} for row in retained_pairs(area))
        )
        insert_rows(connection, ACTUALS, retained_actuals(area))
        insert_rows(
            connection,
            decision_table,
            ({**row, **adapter.identities} for row in retained_decisions(area)),
        )
        analyze(connection, (pair_table, ACTUALS, decision_table))

    captured = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        """Observe real adapter SQL so the proof cannot silently benchmark a disconnected replacement query."""
        if statement.lstrip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    event.listen(adapter.engine, "before_cursor_execute", capture)
    try:
        states = adapter.load(area)
    finally:
        event.remove(adapter.engine, "before_cursor_execute", capture)
    assert len(captured) == 3
    assert {bucket: value["assessed_on"] for bucket, value in states.items()} == {
        0: "2026-10-21",
        1: "2026-10-20",
        2: "2026-10-19",
    }
    with adapter.engine.connect() as connection:
        latest_plans = []
        for statement, parameters in captured:
            assert len(connection.exec_driver_sql(statement, parameters).all()) == 1
            plan = explain(connection, statement, parameters)
            text = json.dumps(plan).lower()
            assert "temp b-tree" not in text
            assert '"node type": "sort"' not in text
            assert '"using_filesort": true' not in text
            latest_plans.append(plan)

    baseline_query = (
        select(decision_table)
        .where(decision_table.c.area == area, *adapter._scope(decision_table))
        .order_by(decision_table.c.day)
    )

    def read_old_history():
        """Reproduce the former full-history fetch and sort on the same warmed database."""
        with adapter.engine.connect() as connection:
            return connection.execute(baseline_query).mappings().all()

    baseline_rows = len(read_old_history())
    assert baseline_rows == (END - START).days * 3 - 3
    baseline_timing = elapsed(read_old_history)
    latest_timing = elapsed(lambda: adapter.load(area))
    selector = ForecastSelection(adapter, area, "primary", "alternative")
    captured.clear()
    event.listen(adapter.engine, "before_cursor_execute", capture)
    start = perf_counter()
    try:
        scored = adapter.update(area, (), (), END, WINDOW_START, selector.decide)
    finally:
        update_ms = (perf_counter() - start) * 1000
        event.remove(adapter.engine, "before_cursor_execute", capture)
    scores = [(sql, params) for sql, params in captured if "GROUP BY" in sql.upper()]
    assert len(scores) == 3
    with adapter.engine.connect() as connection:
        score_plans = [explain(connection, sql, params) for sql, params in scores]
        for plan in score_plans:
            assert (
                "oep_comparison_targets" if identified else PRICE_PAIR_TARGETS.name
            ) in json.dumps(plan)
        for bucket in range(3):
            assert scored[bucket]["primary_mae"] == pytest.approx(0.02)
            assert scored[bucket]["alternative_mae"] == pytest.approx(0.01)
            assert scored[bucket]["matched_hours"] == 720
            assert scored[bucket]["settled_days"] == 30
    assert adapter.load(area) == scored
    with adapter.engine.connect() as connection:
        for table, count in (
            (pair_table, hours * 6),
            (ACTUALS, hours),
            (decision_table, baseline_rows + 3),
        ):
            assert (
                connection.scalar(
                    select(func.count()).select_from(table).where(table.c.area == area)
                )
                == count
            )
    report = {
        "backend": adapter.engine.dialect.name,
        "identified_comparison": identified,
        "period": [START.isoformat(), END.isoformat()],
        "hourly_actuals": hours,
        "forecast_pairs": hours * 6,
        "retained_decisions_before_assessment": baseline_rows,
        "baseline_returned_rows": baseline_rows,
        "latest_returned_rows": 3,
        "baseline_load": baseline_timing,
        "latest_load": latest_timing,
        "daily_assessment_ms": update_ms,
        "latest_query_plans": latest_plans,
        "score_query_plans": score_plans,
    }
    output = Path("results/ci")
    output.mkdir(parents=True, exist_ok=True)
    (
        output
        / f"price-history-{'identified-' if identified else ''}{adapter.engine.dialect.name}.json"
    ).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
