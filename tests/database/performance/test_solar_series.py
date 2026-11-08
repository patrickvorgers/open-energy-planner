"""Repeated multi-year solar evidence retains integrity without thousands of small transactions."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event

from open_energy_planner.solar.calculation.model import version
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import archive_engine
from tests.database.support.backends import BACKENDS


@pytest.mark.parametrize("backend", BACKENDS, ids=[name for name, _url in BACKENDS])
def test_unchanged_four_year_series_rechecks_blocks_in_bounded_queries(
    backend, tmp_path, record_testsuite_property
):
    """All four backends revalidate 1,460 daily blocks with one transaction and at most 16 reads."""
    name, url = backend
    engine = archive_engine(tmp_path, url=url)
    SCHEMA.create_all(engine)
    entity = "sensor.synthetic_" + uuid4().hex
    first = datetime(2022, 1, 1, tzinfo=UTC)
    rows = [
        {
            "start": (first + timedelta(hours=index)).isoformat(),
            "total_kwh": index * 0.1,
            "meter_kwh": index * 0.1,
        }
        for index in range(1460 * 24)
    ]
    reference = version({"entity_id": entity, "hours": rows})
    try:
        store = SolarStore(SqlDocuments(engine, "solar"))
        store.archive_series("history", reference, rows, entity=entity)
        for reopened in (False, True):
            if reopened:
                store = SolarStore(SqlDocuments(engine, "solar"))
            statements, transactions = [], []

            def capture(
                _connection, _cursor, statement, _parameters, _context, _many, captured=statements
            ):
                """Measure actual database statements rather than counting Python helper calls."""
                captured.append(statement.lstrip().split()[0].upper())

            def begin(_connection, captured=transactions):
                """Count physical transaction boundaries on the active backend."""
                captured.append(True)

            event.listen(engine, "before_cursor_execute", capture)
            event.listen(engine, "begin", begin)
            try:
                store.archive_series("history", reference, rows, entity=entity)
            finally:
                event.remove(engine, "before_cursor_execute", capture)
                event.remove(engine, "begin", begin)
            label = f"solar_series_{name}_{'restart' if reopened else 'repeat'}"
            record_testsuite_property(label + "_reads", len(statements))
            record_testsuite_property(label + "_transactions", len(transactions))
            assert set(statements) == {"SELECT"}
            assert len(statements) <= 16
            assert len(transactions) == 1
    finally:
        engine.dispose()
