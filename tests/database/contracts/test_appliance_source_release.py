"""Typed appliance removal and component-forecast membership commit or roll back together."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select

from open_energy_planner.appliances.ev.service import EVService
from open_energy_planner.appliances.forecasting import ConsumerForecasts, parse_selection
from open_energy_planner.appliances.thermal.service import ThermalService
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import DOCUMENTS, SCHEMA
from tests.app.ev.support.devices import catalogue as ev_catalogue
from tests.app.ev.support.devices import settings as ev_settings
from tests.database.contracts.test_thermal_service import catalogue as thermal_catalogue
from tests.database.contracts.test_thermal_service import settings as thermal_settings
from tests.database.support.backends import BACKENDS


@pytest.fixture(params=BACKENDS, ids=[name for name, _url in BACKENDS])
def source_settings(request):
    """Use three independently assigned sources and both typed stores on the real four-backend matrix."""
    backend, url = request.param
    engine = create_engine("sqlite://" if backend == "sqlite" else url)
    SCHEMA.drop_all(engine)
    SCHEMA.create_all(engine)
    catalogue = ev_catalogue()
    thermal_metadata = thermal_catalogue()
    catalogue["entities"].extend(thermal_metadata["entities"])
    catalogue["suggestions"]["consumers"].extend(thermal_metadata["suggestions"]["consumers"])
    catalogue["entities"].append(
        {"entity_id": "sensor.ordinary", "unit": "kWh", "device_class": "energy"}
    )
    catalogue["suggestions"]["consumers"].append({"entity_id": "sensor.ordinary", "upstream": None})
    consumers = ConsumerForecasts(SqlDocuments(engine, "demand"))
    consumers.sync(catalogue)
    consumers.save(
        parse_selection([{"entity_id": "sensor.ordinary", "weather_sensitive": True}]),
        consumers.version,
    )
    appliances = SimpleNamespace(state={"devices": {}})
    ev = EVService(SqlDocuments(engine, "planning"), consumers, appliances)
    ev.configure(ev_settings(), catalogue)
    thermal = ThermalService(SqlDocuments(engine, "planning"), consumers, appliances, ev)
    thermal.configure(thermal_settings(), catalogue)
    try:
        yield engine, catalogue, consumers, ev, thermal
    finally:
        SCHEMA.drop_all(engine)
        engine.dispose()


def document_rows(engine):
    """Compare every immutable document and active pointer, rather than just public summaries."""
    with engine.connect() as connection:
        return connection.execute(
            select(DOCUMENTS).order_by(DOCUMENTS.c.namespace, DOCUMENTS.c.name)
        ).all()


@pytest.mark.parametrize("category", ["ev", "thermal"])
def test_removal_persists_release_and_readdition_without_deleting_history(
    source_settings, category
):
    """A successful removal releases only its own source across reconstruction and preserves both original archives."""
    engine, catalogue, consumers, ev, thermal = source_settings
    service = ev if category == "ev" else thermal
    settings = ev_settings() if category == "ev" else thermal_settings()
    field = "points" if category == "ev" else "stores"
    source = settings[field][0]["energy_entity"]
    before = document_rows(engine)
    selected = consumers.selected
    settings[field] = []
    service.configure(settings, catalogue)
    assert service.state[field] == []
    assert consumers.selected == tuple(row for row in selected if row["entity_id"] != source)
    restored_consumers = ConsumerForecasts(consumers.documents)
    restored_consumers.sync(catalogue)
    assert restored_consumers.selected == consumers.selected
    restored_ev = EVService(ev.documents, restored_consumers, ev.appliances)
    restored = (
        restored_ev
        if category == "ev"
        else ThermalService(thermal.documents, restored_consumers, thermal.appliances, restored_ev)
    )
    assert restored.state[field] == []
    after = {(row.namespace, row.name): row.payload for row in document_rows(engine)}
    for row in before:
        if row.name not in {
            "consumer-settings.json",
            "ev-state.json" if category == "ev" else "thermal-state.json",
        }:
            assert after[(row.namespace, row.name)] == row.payload
    service.configure(ev_settings() if category == "ev" else thermal_settings(), catalogue)
    assert len(service.state[field]) == 1
    assert sum(row["entity_id"] == source for row in consumers.selected) == 1
    assert (
        next(row for row in consumers.selected if row["entity_id"] == "sensor.ordinary")[
            "weather_sensitive"
        ]
        is True
    )


@pytest.mark.parametrize("category", ["ev", "thermal"])
@pytest.mark.parametrize("failure_at", ["consumer-pointer", "typed-pointer", "commit"])
def test_failed_removal_rolls_back_both_configuration_namespaces(
    source_settings, monkeypatch, category, failure_at
):
    """Faults after either archive write or at commit leave all pointers, archives and live settings exactly unchanged."""
    engine, catalogue, consumers, ev, thermal = source_settings
    service = ev if category == "ev" else thermal
    settings = ev_settings() if category == "ev" else thermal_settings()
    field = "points" if category == "ev" else "stores"
    settings[field] = []
    before_rows = document_rows(engine)
    before_state = deepcopy(service.state)
    before_selection = consumers.selected
    before_version = consumers.version
    before_typed_version = service.configuration_version
    target = consumers.documents if failure_at == "consumer-pointer" else service.documents
    pointer = (
        "consumer-settings.json"
        if failure_at == "consumer-pointer"
        else "ev-state.json"
        if category == "ev"
        else "thermal-state.json"
    )
    original_write = target.write

    def interrupted_write(name, value):
        """Fail only after the real pointer write so rollback must cover both already-written records."""
        original_write(name, value)
        if name == pointer:
            raise OSError("Synthetic appliance archive write failure")

    def interrupted_commit(_connection):
        """Reject the real shared database commit before it can become durable."""
        raise OSError("Synthetic appliance commit failure")

    if failure_at == "commit":
        event.listen(engine, "commit", interrupted_commit)
    else:
        monkeypatch.setattr(target, "write", interrupted_write)
    try:
        with pytest.raises(OSError):
            service.configure(settings, catalogue)
    finally:
        if failure_at == "commit":
            event.remove(engine, "commit", interrupted_commit)
    assert document_rows(engine) == before_rows
    assert service.state == before_state
    assert service.configuration_version == before_typed_version
    assert consumers.selected == before_selection
    assert consumers.version == before_version
    restored_consumers = ConsumerForecasts(consumers.documents)
    restored_consumers.sync(catalogue)
    assert restored_consumers.selected == before_selection
    restored_ev = EVService(ev.documents, restored_consumers, ev.appliances)
    restored = (
        restored_ev
        if category == "ev"
        else ThermalService(thermal.documents, restored_consumers, thermal.appliances, restored_ev)
    )
    assert restored.state == before_state
    if failure_at != "commit":
        monkeypatch.setattr(target, "write", original_write)
    service.configure(settings, catalogue)
    assert service.state[field] == []
    removed_source = before_state[field][0]["energy_entity"]
    assert removed_source not in {row["entity_id"] for row in consumers.selected}


def test_related_document_transactions_reject_late_join_and_foreign_engines(source_settings):
    """Enrollment cannot borrow an unrelated transaction or silently span independent databases."""
    engine, _catalogue, consumers, ev, _thermal = source_settings
    before = document_rows(engine)
    with (
        ev.documents.transaction(),
        pytest.raises(ValueError, match="outer transaction boundary"),
        ev.documents.transaction(consumers.documents),
    ):
        pytest.fail("Late enrollment must not execute")
    other_engine = create_engine("sqlite://")
    try:
        with (
            pytest.raises(ValueError, match="share one database engine"),
            ev.documents.transaction(SqlDocuments(other_engine, "demand")),
        ):
            pytest.fail("Foreign-engine enrollment must not execute")
    finally:
        other_engine.dispose()
    assert document_rows(engine) == before


@pytest.mark.parametrize("category", ["ev", "thermal"])
def test_related_settings_cannot_install_memory_before_an_external_commit(
    source_settings, category
):
    """Typed settings reject an already-enrolled caller transaction before writing or installing either configuration."""
    engine, catalogue, consumers, ev, thermal = source_settings
    service = ev if category == "ev" else thermal
    settings = ev_settings() if category == "ev" else thermal_settings()
    settings["points" if category == "ev" else "stores"] = []
    before_rows = document_rows(engine)
    before_state = deepcopy(service.state)
    before_selection = consumers.selected
    with (
        consumers.documents.transaction(service.documents),
        pytest.raises(ValueError, match="must own their commit boundary"),
    ):
        service.configure(settings, catalogue)
    assert document_rows(engine) == before_rows
    assert service.state == before_state
    assert consumers.selected == before_selection
