"""Large forecast archives use bounded backfill and payload-free indexed steady-state expiry."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

from open_energy_planner.storage.adapters import documents as document_adapter
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle import document_retention
from open_energy_planner.storage.adapters.lifecycle.document_retention import maintain_documents
from open_energy_planner.storage.adapters.lifecycle.schema import DOCUMENT_METADATA, DOCUMENTS
from open_energy_planner.storage.adapters.payloads import encode_payload
from tests.database.support.backends import BACKENDS
from tests.database.support.document_retention import finish_index

NOW = datetime(2026, 5, 1, 12, tzinfo=UTC)


def test_steady_state_expiry_never_loads_large_forecast_or_unrelated_archive_bodies(
    settlement, monkeypatch, record_testsuite_property
):
    """A 160-forecast and 400-large-root archive expires one bounded batch with only its small cursor decoded."""
    _runtime, _store, maintenance, engine, _plans = settlement
    solar, unrelated = SqlDocuments(engine, "solar"), SqlDocuments(engine, "contracts")
    padding = "synthetic archived forecast evidence " * 2048
    for index in range(160):
        identity = f"{index:064x}"
        solar.write(f"forecast-{identity}.json", {"version": identity, "detail": padding})
    for index in range(400):
        unrelated.write(f"unrelated-{index:04}.json", {"configuration": padding})
    with engine.begin() as connection:
        connection.execute(
            DOCUMENT_METADATA.update()
            .where(DOCUMENT_METADATA.c.namespace == "solar")
            .values(created_at=(NOW - timedelta(days=31)).isoformat(timespec="microseconds"))
        )
    finish_index(maintenance)
    decoded, statements = [], []
    original_decode = document_adapter.decode_payload

    def decode_small_progress(payload):
        """Count decoded bytes and reject accidental reconstruction of the large retained documents."""
        value = original_decode(payload)
        decoded.append(len(value))
        assert len(value) < 2048
        return value

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        """Observe physical statements instead of implementation helper call counts."""
        statements.append(statement)

    monkeypatch.setattr(document_adapter, "decode_payload", decode_small_progress)
    monkeypatch.setattr(document_retention, "decode_payload", decode_small_progress)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        removed = maintain_documents(maintenance, NOW)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert removed == 128
    assert len(decoded) == 1
    assert len(statements) <= 800
    payload_reads = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT") and "oep_documents.payload" in statement
    ]
    assert len(payload_reads) == 1
    assert sum(solar.exists(f"forecast-{index:064x}.json") for index in range(160)) == 32
    record_testsuite_property("document_retention_statements", len(statements))
    record_testsuite_property("document_retention_decoded_bytes", sum(decoded))


def test_legacy_backfill_decodes_only_128_rows_per_tick_and_resumes_after_restart(
    settlement, monkeypatch, record_testsuite_property
):
    """A thousand large legacy documents are indexed in successive bounded batches rather than rescanned."""
    _runtime, _store, maintenance, engine, _plans = settlement
    payload = encode_payload(json.dumps({"history": "synthetic independent measurement " * 2048}))
    with engine.begin() as connection:
        connection.execute(
            DOCUMENTS.insert(),
            [
                {"namespace": "solar", "name": f"legacy-{index:04}.json", "payload": payload}
                for index in range(1000)
            ],
        )
    decoded = []
    original_decode = document_retention.decode_payload

    def count_backfill(payload):
        """Count the actual legacy bodies reconstructed during one maintenance call."""
        decoded.append(len(payload))
        return original_decode(payload)

    monkeypatch.setattr(document_retention, "decode_payload", count_backfill)
    assert maintain_documents(maintenance, NOW) == 0
    assert len(decoded) == 128
    first_cursor = maintenance.read("archive-retention.json")
    assert first_cursor["stage"] == "documents"
    decoded.clear()
    reopened = SqlDocuments(engine, "savings")
    assert maintain_documents(reopened, NOW) == 0
    assert len(decoded) == 128
    second_cursor = reopened.read("archive-retention.json")
    assert second_cursor["stage"] == "documents"
    assert second_cursor["after"] > first_cursor["after"]
    record_testsuite_property("document_retention_backfill_rows_per_tick", len(decoded))


@pytest.mark.parametrize(
    "settlement", [backend for backend in BACKENDS if backend[0] == "sqlite"], indirect=True
)
def test_expiry_seeks_index_ranges_despite_many_old_roots_and_young_forecasts(
    settlement, record_testsuite_property
):
    """SQLite VM work stays bounded across cursor restart with 40,000 irrelevant metadata rows."""
    _runtime, _store, maintenance, engine, _plans = settlement
    solar = SqlDocuments(engine, "solar")
    old = (NOW - timedelta(days=31)).isoformat(timespec="microseconds")
    young = NOW.isoformat(timespec="microseconds")
    protected = [f"{index:064x}" for index in range(128)]
    for index in range(256):
        identity = f"{index:064x}"
        solar.write(f"forecast-{identity}.json", {"version": identity})
    solar.write("quality-day-frozen.json", {"references": protected})
    with engine.begin() as connection:
        connection.execute(
            DOCUMENT_METADATA.update()
            .where(DOCUMENT_METADATA.c.kind == "forecast")
            .values(created_at=old)
        )
    finish_index(maintenance)
    with engine.begin() as connection:
        connection.execute(
            DOCUMENT_METADATA.insert(),
            [
                {
                    "namespace": "contracts",
                    "name": f"root-{index:05}.json",
                    "created_at": old,
                    "identity": None,
                    "kind": None,
                }
                for index in range(20000)
            ]
            + [
                {
                    "namespace": "solar",
                    "name": f"forecast-{index + 100000:064x}.json",
                    "created_at": young,
                    "identity": f"{index + 100000:064x}",
                    "kind": "forecast",
                }
                for index in range(20000)
            ],
        )
    counts, plans, handles = [], [], set()
    costs = {}

    def observe(_connection, cursor, statement, parameters, _context, _many):
        """Count actual SQLite instructions and inspect the production candidate-selection plan."""
        raw = cursor.connection

        def tick():
            costs[statement] = costs.get(statement, 0) + 100
            counts.append(100)
            return 0

        raw.set_progress_handler(tick, 100)
        handles.add(raw)
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "oep_document_metadata.kind =" in statement
        ):
            probe = raw.cursor()
            try:
                plans.append(
                    probe.execute("EXPLAIN QUERY PLAN " + statement, parameters).fetchall()
                )
            finally:
                probe.close()

    event.listen(engine, "before_cursor_execute", observe)
    try:
        assert maintain_documents(maintenance, NOW) == 0
        first_steps = sum(counts)
        cursor = maintenance.read("archive-retention.json")["after"]
        assert len(cursor) == 4 and cursor[0] == "forecast"
        with engine.begin() as connection:
            connection.execute(
                DOCUMENT_METADATA.insert(),
                [
                    {
                        "namespace": "demand",
                        "name": f"forecast-{index + 200000:064x}.json",
                        "created_at": old,
                        "identity": f"{index + 200000:064x}",
                        "kind": "forecast",
                    }
                    for index in range(20000)
                ],
            )
        counts.clear()
        reopened = SqlDocuments(engine, "savings")
        assert maintain_documents(reopened, NOW) == 128
        second_steps = sum(counts)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
        for handle in handles:
            handle.set_progress_handler(None, 0)
    assert plans
    for plan in plans:
        detail = " ".join(row[3] for row in plan)
        assert "SEARCH oep_document_metadata USING INDEX oep_document_expiry" in detail
        assert "TEMP B-TREE" not in detail
    assert max(first_steps, second_steps) < 100000, sorted(costs.items(), key=lambda row: -row[1])[
        :3
    ]
    assert all(solar.exists(f"forecast-{identity}.json") for identity in protected)
    record_testsuite_property("document_retention_first_page_vm_steps", first_steps)
    record_testsuite_property("document_retention_resumed_page_vm_steps", second_steps)


@pytest.mark.parametrize(
    "settlement", [backend for backend in BACKENDS if backend[0] == "mysql"], indirect=True
)
def test_mysql_resumed_expiry_uses_full_scalar_composite_index_range(
    settlement, record_testsuite_property
):
    """MySQL seeks beyond twenty thousand earlier same-age keys using all four expiry index parts."""
    _runtime, _store, maintenance, engine, _plans = settlement
    solar = SqlDocuments(engine, "solar")
    old = (NOW - timedelta(days=31)).isoformat(timespec="microseconds")
    for index in range(128):
        solar.write(f"forecast-{index + 60000:064x}.json", {"version": f"{index + 60000:064x}"})
    with engine.begin() as connection:
        connection.execute(
            DOCUMENT_METADATA.update()
            .where(DOCUMENT_METADATA.c.kind == "forecast")
            .values(created_at=old)
        )
        connection.execute(
            DOCUMENT_METADATA.insert(),
            [
                {
                    "namespace": "solar",
                    "name": f"forecast-{index:064x}.json",
                    "created_at": old,
                    "identity": f"{index:064x}",
                    "kind": "forecast",
                }
                for index in range(20000)
            ],
        )
    with engine.begin() as connection:
        connection.execute(
            DOCUMENT_METADATA.insert(),
            [
                {
                    "namespace": "solar",
                    "name": f"forecast-{index + 26000:064x}.json",
                    "created_at": old,
                    "identity": None,
                    "kind": None,
                }
                for index in range(20000)
            ],
        )
    maintenance.write(
        "archive-retention.json",
        {"stage": "complete", "after": ["forecast", old, "solar", f"forecast-{25000:064x}.json"]},
    )
    plans = []

    def explain(_connection, cursor, statement, parameters, _context, _many):
        """Inspect the exact production keyset query with the server's optimizer."""
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "oep_document_metadata.kind =" in statement
        ):
            probe = cursor.connection.cursor()
            try:
                probe.execute("EXPLAIN FORMAT=JSON " + statement, parameters)
                plans.append(json.loads(probe.fetchone()[0]))
            finally:
                probe.close()

    event.listen(engine, "before_cursor_execute", explain)
    try:
        assert maintain_documents(maintenance, NOW) == 128
    finally:
        event.remove(engine, "before_cursor_execute", explain)
    assert plans
    pending = [plans[0]]
    tables = []
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if "used_key_parts" in value:
                tables.append(value)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    assert len(tables) == 1
    table = tables[0]
    assert table["key"] == "oep_document_expiry"
    assert table["access_type"] == "range"
    assert table["used_key_parts"] == ["kind", "created_at", "namespace", "name"]
    assert table["rows_examined_per_scan"] < 512
    record_testsuite_property(
        "document_retention_mysql_resume_estimated_rows", table["rows_examined_per_scan"]
    )
