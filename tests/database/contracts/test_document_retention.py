"""Forecast detail expiry preserves live evidence and commits its reference graph atomically."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle import document_retention
from open_energy_planner.storage.adapters.lifecycle.document_retention import maintain_documents
from open_energy_planner.storage.adapters.lifecycle.retention import collect_blocks
from open_energy_planner.storage.adapters.lifecycle.schema import (
    ACTIVATIONS,
    DOCUMENT_LINKS,
    DOCUMENT_METADATA,
    DOCUMENTS,
    PLANS,
)

NOW = datetime(2026, 5, 1, 12, tzinfo=UTC)


def archived(documents, kind, identity, value, *, age_days=31):
    """Write real indexed evidence, then assign its independently specified synthetic storage age."""
    name = f"{kind}-{identity}.json"
    documents.write(name, value)
    with documents.engine.begin() as connection:
        connection.execute(
            DOCUMENT_METADATA.update()
            .where(
                DOCUMENT_METADATA.c.namespace == documents.namespace,
                DOCUMENT_METADATA.c.name == name,
            )
            .values(created_at=(NOW - timedelta(days=age_days)).isoformat(timespec="microseconds"))
        )
    return name


def finish_index(documents, now=NOW):
    """Resume maintenance until both legacy owner classes have been indexed without expiring data."""
    for _attempt in range(64):
        assert maintain_documents(documents, now) == 0
        if documents.read("archive-retention.json")["stage"] == "complete":
            return
    pytest.fail("Synthetic reference-index backfill did not finish")


def finish_collection(documents, now=NOW):
    """Allow parent-first collection to revisit lexical predecessors after removing their owners."""
    for _attempt in range(8):
        maintain_documents(documents, now)


def test_rewriting_a_pointer_preserves_first_storage_age_and_replaces_its_links(settlement):
    """A mutable pointer retains its age while only its new target remains protected after restart."""
    _runtime, _store, maintenance, engine, _plans = settlement
    demand = SqlDocuments(engine, "demand")
    first, second = "a" * 64, "b" * 64
    first_name = archived(demand, "forecast", first, {"version": first})
    second_name = archived(demand, "forecast", second, {"version": second})
    demand.write("current-model.json", {"reference": first})
    with engine.connect() as connection:
        original_age = connection.scalar(
            select(DOCUMENT_METADATA.c.created_at).where(
                DOCUMENT_METADATA.c.namespace == "demand",
                DOCUMENT_METADATA.c.name == "current-model.json",
            )
        )
    demand.write("current-model.json", {"reference": second})
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(DOCUMENT_METADATA.c.created_at).where(
                    DOCUMENT_METADATA.c.namespace == "demand",
                    DOCUMENT_METADATA.c.name == "current-model.json",
                )
            )
            == original_age
        )
    finish_index(maintenance)
    reopened = SqlDocuments(engine, "savings")
    finish_collection(reopened)
    assert not demand.exists(first_name)
    assert demand.read(second_name) == {"version": second}


def test_legacy_age_starts_at_indexing_and_exactly_thirty_days_is_not_expired(settlement):
    """An old embedded model date cannot shorten the full grace period of a newly indexed archive."""
    _runtime, _store, maintenance, engine, _plans = settlement
    identity = "c" * 64
    name = f"forecast-{identity}.json"
    value = {"version": identity, "issued_at": "2001-01-01T00:00:00+00:00"}
    with engine.begin() as connection:
        connection.execute(
            DOCUMENTS.insert().values(namespace="solar", name=name, payload=json.dumps(value))
        )
    finish_index(maintenance)
    solar = SqlDocuments(engine, "solar")
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(DOCUMENT_METADATA.c.created_at).where(
                    DOCUMENT_METADATA.c.namespace == "solar", DOCUMENT_METADATA.c.name == name
                )
            )
            == "2026-05-01T12:00:00.000000+00:00"
        )
    assert maintain_documents(maintenance, NOW + timedelta(days=30)) == 0
    assert solar.read(name) == value
    assert maintain_documents(maintenance, NOW + timedelta(days=30, microseconds=1)) == 1
    assert not solar.exists(name)


def test_incomplete_index_never_expires_a_target_whose_legacy_root_is_not_indexed(
    settlement, monkeypatch
):
    """Small resumable batches cannot delete a forecast before discovering a later daily-quality root."""
    _runtime, _store, maintenance, engine, _plans = settlement
    solar = SqlDocuments(engine, "solar")
    identity = "d" * 64
    name = archived(solar, "forecast", identity, {"version": identity})
    with engine.begin() as connection:
        connection.execute(
            DOCUMENTS.insert(),
            [
                {"namespace": "solar", "name": f"older-root-{index}.json", "payload": "{}"}
                for index in range(8)
            ]
            + [
                {
                    "namespace": "solar",
                    "name": "quality-day-2026-04-01.json",
                    "payload": json.dumps({"solar_evidence": {"version": identity}}),
                }
            ],
        )
    monkeypatch.setattr(document_retention, "REFERENCE_BACKFILL_BATCH_ROWS", 2)
    assert maintain_documents(maintenance, NOW) == 0
    assert maintenance.read("archive-retention.json")["stage"] == "documents"
    assert solar.exists(name)
    finish_index(maintenance)
    finish_collection(maintenance)
    assert solar.read(name) == {"version": identity}


def test_corrupt_legacy_document_rolls_back_the_whole_backfill_batch(settlement):
    """A valid earlier row cannot leave a partial index or advanced cursor when the next JSON is corrupt."""
    _runtime, _store, maintenance, engine, _plans = settlement
    with engine.begin() as connection:
        connection.execute(
            DOCUMENTS.insert(),
            [
                {"namespace": "solar", "name": "a-legacy.json", "payload": "{}"},
                {"namespace": "solar", "name": "b-legacy.json", "payload": "broken-json"},
            ],
        )
    with pytest.raises(ValueError):
        maintain_documents(maintenance, NOW)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(DOCUMENT_METADATA.c.name).where(
                    DOCUMENT_METADATA.c.namespace == "solar",
                    DOCUMENT_METADATA.c.name == "a-legacy.json",
                )
            )
            is None
        )
    assert not maintenance.exists("archive-retention.json")


@pytest.mark.parametrize(
    "progress",
    [
        "not-a-progress-record",
        {"stage": "unknown", "after": None},
        {"stage": "complete", "after": "bad"},
    ],
)
def test_corrupt_progress_cannot_enable_expiry(settlement, progress):
    """An invalid phase or cursor remains an explicit failure while old forecast evidence survives."""
    _runtime, _store, maintenance, engine, _plans = settlement
    solar = SqlDocuments(engine, "solar")
    name = archived(solar, "forecast", "e" * 64, {"kwh": 0.25})
    maintenance.write("archive-retention.json", progress)
    with pytest.raises(ValueError, match="Invalid document retention"):
        maintain_documents(maintenance, NOW)
    assert solar.read(name) == {"kwh": 0.25}
    assert maintenance.read("archive-retention.json") == progress


def test_daily_model_quality_and_original_plan_roots_protect_their_recursive_evidence(settlement):
    """Current models, frozen quality and shared original-plan leaves retain exact old dependencies."""
    _runtime, _store, maintenance, engine, plans = settlement
    solar, demand = SqlDocuments(engine, "solar"), SqlDocuments(engine, "demand")
    first, alternative, parameters, daily, plan_forecast, orphan = (
        character * 64 for character in "abcdef"
    )
    first_name = archived(solar, "forecast", first, {"version": first, "alternative": alternative})
    alternative_name = archived(solar, "forecast", alternative, {"version": alternative})
    companion = archived(solar, "quality-candidates", first, {"forecasts": [first, alternative]})
    parameter_name = archived(demand, "model-parameters", parameters, {"coefficient": 0.375})
    daily_name = archived(demand, "daily-models", daily, {"model_parameters": parameters})
    plan_name = archived(solar, "forecast", plan_forecast, {"version": plan_forecast})
    orphan_name = archived(solar, "forecast", orphan, {"version": orphan})
    solar.write("quality-day-2026-04-01.json", {"solar_evidence": {"version": first}})
    demand.write("current-model.json", {"reference": daily})
    original = plans.get_plan("original-plan")
    plan = {**deepcopy(original), "plan_id": "retention-reference-plan"}
    plan["input_snapshot"]["solar_evidence"] = {"version": plan_forecast}
    plans.state["plan"] = plan
    plans.save()
    finish_index(maintenance)
    assert maintain_documents(maintenance, NOW) == 1
    assert not solar.exists(orphan_name)
    assert plans.get_plan(plan["plan_id"]) == plan
    assert all(solar.exists(name) for name in (first_name, alternative_name, companion, plan_name))
    assert demand.read(parameter_name) == {"coefficient": 0.375}
    assert demand.read(daily_name) == {"model_parameters": parameters}
    solar.delete("quality-day-2026-04-01.json")
    demand.delete("current-model.json")
    finish_collection(maintenance)
    assert all(not solar.exists(name) for name in (first_name, alternative_name, companion))
    assert not demand.exists(parameter_name) and not demand.exists(daily_name)
    assert solar.exists(plan_name)
    plans.state["plan"] = None
    plans.save()
    with engine.begin() as connection:
        connection.execute(
            ACTIVATIONS.update()
            .where(ACTIVATIONS.c.plan_id == plan["plan_id"])
            .values(plan_id=None)
        )
        connection.execute(PLANS.delete().where(PLANS.c.plan_id == plan["plan_id"]))
        for _round in range(4):
            collect_blocks(connection)
    finish_collection(maintenance)
    assert not solar.exists(plan_name)


def test_a_new_companion_protects_the_whole_old_forecast_family(settlement):
    """A 31-day forecast survives until its 29-day companion has also exceeded thirty days."""
    _runtime, _store, maintenance, engine, _plans = settlement
    solar = SqlDocuments(engine, "solar")
    identity = "a" * 64
    forecast = archived(solar, "forecast", identity, {"version": identity})
    companion = archived(
        solar, "quality-candidates", identity, {"forecasts": [identity]}, age_days=29
    )
    finish_index(maintenance)
    assert maintain_documents(maintenance, NOW) == 0
    assert maintain_documents(maintenance, NOW + timedelta(days=1)) == 0
    assert solar.exists(forecast) and solar.exists(companion)
    assert maintain_documents(maintenance, NOW + timedelta(days=1, microseconds=1)) == 2
    assert not solar.exists(forecast) and not solar.exists(companion)


def test_a_batch_never_deletes_more_than_128_documents_across_companion_families(settlement):
    """A 260-document archive expires through several resumable batches without splitting companions."""
    _runtime, _store, maintenance, engine, _plans = settlement
    solar = SqlDocuments(engine, "solar")
    names = []
    for index in range(130):
        identity = f"{index:064x}"
        names.append(archived(solar, "forecast", identity, {"version": identity}))
        names.append(archived(solar, "quality-candidates", identity, {"forecasts": [identity]}))
    finish_index(maintenance)
    removed = []
    for _round in range(8):
        deleted = maintain_documents(maintenance, NOW)
        assert 0 <= deleted <= 128
        removed.append(deleted)
        for index in range(0, len(names), 2):
            assert solar.exists(names[index]) == solar.exists(names[index + 1])
    assert removed[0] > 0 and sum(removed) == 260
    assert all(not solar.exists(name) for name in names)


def test_cross_family_cycles_are_retained_and_old_assessment_chains_can_be_collected(settlement):
    """Conservative cyclic ownership never loses data while obsolete pointer-to-assessment chains expire."""
    _runtime, _store, maintenance, engine, _plans = settlement
    solar = SqlDocuments(engine, "solar")
    first, second, pointer, assessment, fit = (character * 64 for character in "abcde")
    first_name = archived(solar, "forecast", first, {"other": second})
    second_name = archived(solar, "forecast", second, {"other": first})
    obsolete = [
        archived(solar, "historical-assessment-current", pointer, assessment),
        archived(solar, "historical-assessment", assessment, {"training_reference": fit}),
        archived(solar, "historical-fit", fit, {"mae_kwh": 0.125}),
    ]
    finish_index(maintenance)
    finish_collection(maintenance)
    assert solar.read(first_name) == {"other": second}
    assert solar.read(second_name) == {"other": first}
    assert all(not solar.exists(name) for name in obsolete)


def test_interrupted_family_deletion_rolls_back_documents_links_metadata_and_cursor(settlement):
    """A failure after deleting the body cannot publish a missing forecast or a partially removed index."""
    _runtime, _store, maintenance, engine, _plans = settlement
    solar = SqlDocuments(engine, "solar")
    identity = "f" * 64
    name = archived(solar, "forecast", identity, {"version": identity, "kwh": 0.375})
    finish_index(maintenance)
    before = maintenance.read("archive-retention.json")

    def interrupt_metadata_delete(_connection, _cursor, statement, _parameters, _context, _many):
        """Interrupt the actual family transaction after body/link deletion but before metadata commits."""
        if statement.lstrip().upper().startswith("DELETE") and "oep_document_metadata" in statement:
            raise RuntimeError("Synthetic interrupted family deletion")

    event.listen(engine, "before_cursor_execute", interrupt_metadata_delete)
    try:
        with pytest.raises(RuntimeError, match="Synthetic interrupted family deletion"):
            maintain_documents(maintenance, NOW)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt_metadata_delete)
    assert solar.read(name) == {"version": identity, "kwh": 0.375}
    assert maintenance.read("archive-retention.json") == before
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(DOCUMENT_METADATA.c.name).where(
                    DOCUMENT_METADATA.c.namespace == "solar", DOCUMENT_METADATA.c.name == name
                )
            )
            == name
        )
        assert (
            connection.scalar(
                select(DOCUMENT_LINKS.c.target_hash).where(
                    DOCUMENT_LINKS.c.owner_kind == "document",
                    DOCUMENT_LINKS.c.owner_namespace == "solar",
                    DOCUMENT_LINKS.c.owner_name == name,
                )
            )
            == identity
        )
    assert maintain_documents(maintenance, NOW) == 1
