"""Keep current-history activation and validated chunk reuse fail closed on every supported backend."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.appliances.forecasting import ConsumerForecasts, parse_selection
from open_energy_planner.demand import runtime as runtime_module
from open_energy_planner.demand.calculation.model import EnergyTerm, ExcludedLoad, digest
from open_energy_planner.demand.runtime import DemandRuntime
from open_energy_planner.demand.storage import DemandStore
from open_energy_planner.history.importer import (
    CumulativeHistory,
    EnergyEndpoint,
    HistoryContinuityError,
    HistoryImport,
    parse_report,
)
from open_energy_planner.history.storage import HistoryArchive
from open_energy_planner.storage.adapters.documents import SqlDocuments
from tests.app.demand.history_support import history_report
from tests.app.demand.test_demand import config
from tests.database.contracts.test_document_backends import archive

__all__ = ["archive"]


@pytest.mark.parametrize("owner", ["consumer", "household", "exclusion"])
@pytest.mark.parametrize("damage", ["quarantined", "unreadable"])
def test_removed_meter_warnings_follow_selection_without_erasing_durable_faults(
    archive, owner, damage
):
    """Hide removed-meter faults while retaining selected warnings and recovering faults on reselection and restart."""
    engine, _root = archive
    store = DemandStore(SqlDocuments(engine, "demand"))
    now = datetime(2026, 6, 1, tzinfo=UTC)
    entities = ("sensor.kept", "sensor.removed")
    importer = HistoryImport(HistoryArchive(store.documents))
    evidence = {}
    for entity in entities:
        history = CumulativeHistory(
            entity, (EnergyEndpoint(datetime(2026, 5, 1, tzinfo=UTC), 100),)
        )
        pending = importer.request(entity, "synthetic-selection", now)
        report = history_report(pending, history)
        importer.receive(pending, parse_report(report), now)
        if damage == "quarantined":
            changed = deepcopy(report)
            changed["statistics_identity"]["metadata_id"] = 18
            with pytest.raises(HistoryContinuityError):
                importer.receive(pending, parse_report(changed), now)
            pointer = "history-import-quarantine-" + digest(entity) + ".json"
            reference = store.documents.read(pointer)
            name = "history-import-fault-" + reference + ".json"
        else:
            pointer = "history-import-current-" + digest(entity) + ".json"
            reference = store.documents.read(pointer)
            name = "history-import-" + reference + ".json"
            damaged = store.documents.read(name)
            damaged["entity_id"] = "sensor.incompatible"
            store.documents.write(name, damaged)
        evidence[pointer] = store.documents.read(pointer)
        evidence[name] = store.documents.read(name)
    issue = (
        "history_continuity_unavailable" if damage == "quarantined" else "history_cache_unreadable"
    )
    if owner == "consumer":
        runtime = ConsumerForecasts(store.documents)
        runtime.sync(
            {
                "entities": [{"entity_id": entity} for entity in entities],
                "suggestions": {
                    "consumers": [{"entity_id": entity, "upstream": None} for entity in entities]
                },
            }
        )

        def select(selected_entities):
            """Save actual Energy membership through the consumer runtime's public operation."""
            runtime.save(
                parse_selection(
                    [
                        {"entity_id": entity, "weather_sensitive": False}
                        for entity in selected_entities
                    ]
                ),
                runtime.version,
            )

        def status():
            """Read the actual consumer document rather than its private importer cache."""
            return runtime.document()["history_issues"]

        select(entities)
    else:
        runtime = DemandRuntime(store)

        def select(selected_entities):
            """Persist and configure household terms or exclusions without editing source faults."""
            if owner == "household":
                configuration = replace(
                    config(), terms=tuple(EnergyTerm(entity, 1) for entity in selected_entities)
                )
            else:
                configuration = replace(
                    config(),
                    exclusions=tuple(
                        ExcludedLoad("synthetic-" + str(index), entity)
                        for index, entity in enumerate(selected_entities)
                    ),
                )
            store.save(configuration)
            runtime.configure(configuration)

        def status():
            """Read the current demand document, including the configured measurement boundary."""
            return runtime.document(now)["history_issues"]

        select(entities)
    assert status() == {entity: issue for entity in entities}
    select(("sensor.kept",))
    assert status() == {"sensor.kept": issue}
    select(entities)
    assert status() == {entity: issue for entity in entities}
    restarted = (
        ConsumerForecasts(store.documents)
        if owner == "consumer"
        else DemandRuntime(DemandStore(store.documents))
    )
    public = restarted.document() if owner == "consumer" else restarted.document(now)
    assert public["history_issues"] == {entity: issue for entity in entities}
    assert all(store.documents.read(name) == value for name, value in evidence.items())


def current_history(archive, monkeypatch):
    """Build a complete current source through real range/chunk envelopes and a fixed UTC clock."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    monkeypatch.setattr(runtime_module, "datetime", SimpleNamespace(now=lambda _zone: now))
    engine, _root = archive
    store = DemandStore(SqlDocuments(engine, "demand"))
    store.save(config())
    runtime = DemandRuntime(store)
    anchor = datetime(2026, 5, 1, tzinfo=UTC)
    history = CumulativeHistory(
        "sensor.house",
        tuple(EnergyEndpoint(anchor + timedelta(hours=index), 100 + index) for index in range(4)),
    )
    last = None
    while pending := runtime.request(now):
        runtime.receive(pending["version"], parse_report(history_report(pending, history)), now)
        last = pending
    return store, runtime, history, now, last


@pytest.mark.parametrize("unavailable", ["missing", "partial", "mixed_target", "quarantined"])
def test_definition_change_cannot_activate_incomplete_or_faulted_sources(
    archive, monkeypatch, unavailable
):
    """Valid old rows alone cannot establish a new complete, current, source-consistent boundary."""
    store, runtime, history, now, last = current_history(archive, monkeypatch)
    previous_key = "household-history-cohort-" + store.current.version + ".json"
    previous = store.documents.read(previous_key)
    terms = (*store.current.terms, EnergyTerm("sensor.added", 1))
    if unavailable in ("partial", "mixed_target"):
        importer = HistoryImport(HistoryArchive(store.documents))
        added = CumulativeHistory(
            "sensor.added",
            tuple(
                EnergyEndpoint(datetime(year, 5, 1, hour, tzinfo=UTC), (year - 2020) * 100 + hour)
                for year in (2025, 2026)
                for hour in (0, 1)
            ),
        )
        observed = now if unavailable == "partial" else now - timedelta(days=1)
        while pending := importer.request("sensor.added", store.current.version, observed):
            importer.receive(pending, parse_report(history_report(pending, added)), observed)
            if unavailable == "partial" and pending.get("import_year") == 2025:
                break
    elif unavailable == "quarantined":
        changed_source = history_report(last, history)
        changed_source["statistics_identity"]["metadata_id"] = 18
        with pytest.raises(HistoryContinuityError):
            runtime.history_import.receive(last, parse_report(changed_source), now)
        terms = store.current.terms
    changed = replace(store.current, annual_kwh=5000, terms=terms)
    store.save(changed)
    runtime.configure(changed)
    assert runtime.histories == {}
    assert not store.documents.exists("household-history-cohort-" + changed.version + ".json")
    assert store.documents.read(previous_key) == previous


def test_failed_activation_pointer_write_is_retryable_without_replacing_old_evidence(
    archive, monkeypatch
):
    """Activation publication rolls back before exposing new state, and restart can retry the same checkpoints."""
    store, runtime, history, _now, _last = current_history(archive, monkeypatch)
    previous_key = "household-history-cohort-" + store.current.version + ".json"
    previous = store.documents.read(previous_key)
    changed = replace(store.current, annual_kwh=5000)
    store.save(changed)
    key = "household-history-cohort-" + changed.version + ".json"
    original = store.documents.write

    def fail(name, value):
        """Interrupt only the new accepted-cohort publication."""
        if name == key:
            raise OSError("Synthetic cohort activation failure")
        return original(name, value)

    with monkeypatch.context() as fault:
        fault.setattr(store.documents, "write", fail)
        runtime.configure(changed)
    assert runtime.histories == {}
    assert not store.documents.exists(key)
    assert store.documents.read(previous_key) == previous
    restarted = DemandRuntime(DemandStore(store.documents))
    assert restarted.histories == {"sensor.house": history}
    assert store.documents.read(key) == previous


@pytest.mark.parametrize("altered", ["range", "identity", "entity"])
def test_warm_chunk_cache_does_not_bypass_source_or_range_binding(archive, monkeypatch, altered):
    """A validated reference cannot authorize another sensor, identity or descriptor range."""
    _store, runtime, _history, _now, _last = current_history(archive, monkeypatch)
    state = deepcopy(runtime.history_import.states["sensor.house"])
    descriptor = state["chunks"][0]
    if altered == "range":
        descriptor["end"] = (
            datetime.fromisoformat(descriptor["end"]) - timedelta(hours=1)
        ).isoformat()
    elif altered == "identity":
        state["statistics_identity"]["metadata_id"] = 18
    else:
        state["entity_id"] = "sensor.other"
    with pytest.raises(ValueError, match="cached energy history chunk"):
        runtime.history_import._chunk(state, descriptor)


@pytest.mark.parametrize("reload", ["restore", "restart"])
def test_history_reload_revalidates_durable_chunks_after_warm_cache(archive, monkeypatch, reload):
    """A warm normalized copy cannot conceal subsequently corrupted durable evidence from a reload."""
    store, runtime, _history, _now, _last = current_history(archive, monkeypatch)
    descriptor = runtime.history_import.states["sensor.house"]["chunks"][0]
    name = "history-import-chunk-" + descriptor["reference"] + ".json"
    corrupted = store.documents.read(name)
    corrupted["hours"][0][1] += 999
    store.documents.write(name, corrupted)
    if reload == "restore":
        assert runtime.history_import.restore(("sensor.house",)) == {}
        assert runtime.history_import.issues["sensor.house"] == "history_cache_unreadable"
    else:
        restarted = DemandRuntime(DemandStore(store.documents))
        assert restarted.histories == {}
        assert restarted.history_issues["sensor.house"] == "history_cache_unreadable"
