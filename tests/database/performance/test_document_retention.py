"""Large forecast archives use bounded backfill and payload-free indexed steady-state expiry."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import event

from open_energy_planner.storage.adapters import documents as document_adapter
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle import document_retention
from open_energy_planner.storage.adapters.lifecycle.document_retention import maintain_documents
from open_energy_planner.storage.adapters.lifecycle.schema import DOCUMENT_METADATA, DOCUMENTS
from open_energy_planner.storage.adapters.payloads import encode_payload

NOW = datetime(2026, 5, 1, 12, tzinfo=UTC)


def finish_index(documents):
    """Complete the synthetic archive's resumable indexing before measuring ordinary cleanup."""
    for _attempt in range(32):
        assert maintain_documents(documents, NOW) == 0
        if documents.read("archive-retention.json")["stage"] == "complete":
            return
    raise AssertionError("Synthetic reference-index backfill did not finish")


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
