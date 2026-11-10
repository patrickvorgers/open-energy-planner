"""Every durable typed store shares the same SQLAlchemy database and preserves atomic versions/pointers."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import DBAPIError

from open_energy_planner.contracts.storage import ContractStore
from open_energy_planner.contracts.wire import contract_document, parse_contract
from open_energy_planner.demand.storage import DemandStore
from open_energy_planner.installation.storage import InstallationStore, parse
from open_energy_planner.optimization.domain import Strategy
from open_energy_planner.optimization.storage import StrategyStore
from open_energy_planner.prices.storage.adapters.store import PriceStore
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.storage.adapters.documents import DOCUMENT_NAMESPACES, SqlDocuments
from open_energy_planner.storage.adapters.lifecycle import sqlalchemy as archive_adapter
from open_energy_planner.storage.adapters.lifecycle.schema import DOCUMENTS, SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SqlPlanRepository,
    archive_engine,
)
from open_energy_planner.storage.counters import CounterStore
from tests.app.appliance.test_appliance_learning import ENTITY, POWER, readings
from tests.app.appliance.test_appliance_learning import NOW as LEARNING_NOW
from tests.app.contract.support.settings import settings
from tests.app.contract.test_contract_bonuses import measured_inputs
from tests.app.contract.test_contract_prices import NOW, contract, price_series
from tests.app.demand.test_demand import config
from tests.app.solar.support import plant
from tests.database.support.backends import BACKENDS


def test_solar_feedback_receipts_reuse_segments_and_survive_restart(archive, monkeypatch):
    """Every backend retains exact boolean/unknown feedback and atomically rejects an interrupted pointer update."""
    from open_energy_planner.solar.curtailment import (
        CurtailmentHistory,
        archive_feedback,
        load_feedback,
        save_feedback,
    )
    from tests.app.solar.support import START

    engine, _root = archive
    store = SolarStore(SqlDocuments(engine, "solar"))
    source = replace(plant().sources[0], curtailment_entity_id="binary_sensor.curtailed")
    configuration = replace(plant(), sources=(source,))
    store.save(configuration)
    first = CurtailmentHistory(
        source.curtailment_entity_id, START + timedelta(hours=1), ((START, False),)
    )
    accepted = save_feedback(store, None, first, START - timedelta(hours=1))
    first_accepted = accepted
    first_reference = archive_feedback(store, accepted)
    blocks = list(store.documents.iter_prefix("curtailment-segment-"))
    assert len(blocks) == 1
    following = CurtailmentHistory(source.curtailment_entity_id, START + timedelta(hours=2), ())
    accepted = save_feedback(store, accepted, following, first.checked_until)
    assert list(store.documents.iter_prefix("curtailment-segment-")) == blocks
    restored = SolarStore(SqlDocuments(engine, "solar"))
    assert restored.current == configuration
    assert (
        load_feedback(
            restored.documents,
            source.curtailment_entity_id,
            START + timedelta(hours=3),
            reference=first_reference,
        )
        == first_accepted
    )
    assert (
        load_feedback(restored.documents, source.curtailment_entity_id, START + timedelta(hours=3))
        == accepted
    )
    original_write = store.documents.write

    def interrupted(name, value):
        """Fail at publication after the new immutable receipt has been prepared."""
        if name.startswith("curtailment-current-"):
            raise OSError("synthetic interrupted feedback write")
        original_write(name, value)

    monkeypatch.setattr(store.documents, "write", interrupted)
    next_read = CurtailmentHistory(
        source.curtailment_entity_id,
        START + timedelta(hours=3),
        ((START + timedelta(hours=2), None),),
    )
    with pytest.raises(OSError):
        save_feedback(store, accepted, next_read, following.checked_until)
    assert (
        load_feedback(restored.documents, source.curtailment_entity_id, START + timedelta(hours=4))
        == accepted
    )


def test_solar_raw_sum_evidence_and_local_conflicts_survive_restart(archive):
    """Every backend preserves raw endpoints, reset-safe sums and original conflict anchors across refreshes."""
    from datetime import UTC, datetime

    from open_energy_planner.solar.runtime import SolarRuntime
    from open_energy_planner.solar.storage.history import parse_report
    from tests.app.solar.history_support import HISTORY_NOW, discover

    engine, _root = archive
    documents = SqlDocuments(engine, "solar")
    store = SolarStore(documents)
    store.save(plant())
    runtime = SolarRuntime(store)
    discover(runtime, first="2026-01-01T00:00:00+00:00")
    pending = runtime.request(HISTORY_NOW)
    start = datetime(2026, 1, 1, 9, tzinfo=UTC)
    raw = [
        [start.timestamp() - 16, 100, 800],
        [(start + timedelta(hours=3)).timestamp() + 64, 112, 0],
        [(start + timedelta(hours=4)).timestamp(), 115, None],
    ]

    def capture(request, rows):
        """Send source evidence independently of the normalized typed view."""
        report = parse_report(
            {
                "request_version": request["version"],
                "basis": "solar_history_chunk",
                "entity_id": "sensor.pv0",
                "statistics_identity": {"metadata_id": 7, "source": "recorder", "unit": "kWh"},
                "unit": "kWh",
                "hours": rows,
            }
        )
        runtime.receive(request["version"], report.history, now, report=report)

    now = HISTORY_NOW
    capture(pending, raw)
    original = runtime.history_import.states["sensor.pv0"]["chunks"][0]["reference"]
    evidence = documents.read("history-import-chunk-" + original + ".json")
    assert evidence["hours"] == raw
    assert evidence["alignment"] == "nearest_utc_hour_64s"
    reopened = SolarRuntime(SolarStore(SqlDocuments(engine, "solar")))
    assert reopened.histories == runtime.histories
    original_history = reopened.histories.copy()
    assert [hour.total_kwh for hour in reopened.histories["sensor.pv0"].hours] == [100, 112, 115]
    runtime = reopened
    now += timedelta(days=1)
    changed = [raw[0], [raw[1][0], 113, None], raw[2]]
    capture(runtime.request(now), changed)
    assert [hour.total_kwh for hour in runtime.histories["sensor.pv0"].hours] == [100, 115]
    assert runtime.history_issues == {}
    runtime = SolarRuntime(SolarStore(SqlDocuments(engine, "solar")))
    assert runtime.history_import.states["sensor.pv0"]["conflicts"] == [
        [(start + timedelta(hours=3)).timestamp(), 112]
    ]
    now += timedelta(days=1)
    capture(runtime.request(now), changed)
    assert [hour.total_kwh for hour in runtime.histories["sensor.pv0"].hours] == [100, 115]
    now += timedelta(days=1)
    capture(runtime.request(now), raw)
    assert runtime.histories == original_history
    assert runtime.history_import.states["sensor.pv0"]["conflicts"] == []
    assert documents.read("history-import-chunk-" + original + ".json") == evidence
    assert SolarRuntime(SolarStore(SqlDocuments(engine, "solar"))).histories == runtime.histories


def test_daily_demand_accuracy_preserves_originals_after_restart(archive):
    """All four backends retain daily forecast evidence and admission after reopening documents."""
    from open_energy_planner.forecast_selection.accuracy import choose_daily
    from tests.app.forecast.support.daily_accuracy import START, forecasts, measurements

    engine, _root = archive
    documents = SqlDocuments(engine, "demand")
    for offset in range(6):
        now = START + timedelta(days=offset)
        selected = choose_daily(documents, "washer", "UTC", forecasts(now), measurements(now), now)
    originals = list(documents.iter_prefix("accuracy-day-"))
    assert originals and selected["selected"] == "candidate"
    reopened = SqlDocuments(engine, "demand")
    result = choose_daily(reopened, "washer", "UTC", forecasts(now), measurements(now), now)
    assert result == selected
    assert list(reopened.iter_prefix("accuracy-day-")) == originals


def test_solar_import_forecast_and_accuracy_contracts_survive_restart(archive):
    """All four backends retain edited geometry, original imports and immutable HA/accuracy evidence."""
    from open_energy_planner.solar.calculation.model import SolarImport, SolarPanel
    from open_energy_planner.solar.runtime import SolarRuntime
    from open_energy_planner.solar.source_selection import choose_sources
    from tests.app.solar.support import START
    from tests.app.solar.test_source_selection import prediction, provider

    engine, _root = archive
    documents = SqlDocuments(engine, "solar")
    store = SolarStore(documents)
    original = SolarPanel("entry.plane", 1000, 205, 45)
    configuration = plant()
    edited = replace(
        configuration.sources[0],
        panels=(replace(original, azimuth=210),),
        ha_imports=(SolarImport("forecast_solar", "entry", (original,), 12000),),
        forecast_source="ha",
    )
    configuration = replace(configuration, sources=(edited,))
    store.save(configuration)
    runtime = SolarRuntime(store)
    runtime.save_ha({"sensor.pv0": (provider(),)})
    choose_sources(
        store,
        configuration,
        prediction(START, 0.5, configuration=configuration),
        None,
        (),
        {},
        {},
        START,
    )
    restored = SolarStore(SqlDocuments(engine, "solar"))
    assert restored.current == configuration
    source = restored.current.sources[0]
    assert source.panels[0].azimuth == 210
    assert source.ha_imports[0].panels[0].azimuth == 205
    assert source.forecast_source == "ha"
    observations = list(restored.documents.iter_prefix("ha-first-"))
    assert len(observations) == 1
    assert observations[0]["provider_issued_at"] is None
    assert observations[0]["configuration_revision"] == "configuration-a"
    assert observations[0]["steps"][0]["kwh"] == 0.4
    captures = list(restored.documents.iter_prefix("source-day-"))
    assert len(captures) == 1
    assert captures[0]["models"]["pvwatts"] == [0.5] * 24


def test_hourly_learning_checkpoint_weight_archive_and_rollback(archive, monkeypatch):
    """All real backends restore weighted estimates and atomically roll back failed batch checkpoints."""
    from copy import deepcopy

    from open_energy_planner.appliances.cycles import history
    from open_energy_planner.appliances.cycles.service import ApplianceService
    from open_energy_planner.appliances.forecasting import ConsumerForecasts
    from tests.app.appliance.test_history_learning import begin, hourly_rows, upload

    engine, _root = archive
    consumers = ConsumerForecasts(SqlDocuments(engine, "demand"))
    consumers.sync(
        {
            "entities": [{"entity_id": ENTITY, "name": "Washer"}],
            "suggestions": {
                "consumers": [{"entity_id": ENTITY, "upstream": None, "power_entity": POWER}]
            },
        }
    )
    consumers.save([{"entity_id": ENTITY, "weather_sensitive": False}], consumers.version)
    tasks = ApplianceService(SqlDocuments(engine, "planning"), consumers)
    start = LEARNING_NOW - timedelta(days=500)
    begin(tasks, start, start + timedelta(days=8))
    rows = hourly_rows(start, [0, 0, 500, 0, 0, 1000, 0])
    upload(tasks, rows)
    reopened = ApplianceService(SqlDocuments(engine, "planning"), consumers)
    assert reopened.state == json.loads(json.dumps(tasks.state))
    assert (
        reopened.document()["devices"][0]["history_request"]["start"]
        == (start + timedelta(days=7)).isoformat()
    )
    archived = list(reopened.documents.iter_prefix("appliance-hourly-"))
    assert len(archived) == 2
    assert all(record["quality"] == "hourly_estimate" for record in archived)
    snapshots = list(reopened.documents.iter_prefix("appliance-state-"))
    reopened.learn_history(ENTITY, POWER, [], False, LEARNING_NOW)
    assert list(reopened.documents.iter_prefix("appliance-state-")) == snapshots
    core = reopened.documents.read("appliance-state.json")["devices"][ENTITY]
    assert "learning_history" not in core and "history_import" not in core
    progress = reopened.documents.read("appliance-progress.json")["devices"][ENTITY]
    assert progress["history_import"]["cursor"] == (start + timedelta(days=7)).isoformat()
    assert progress["learning_history"]["usable_intervals"] == 0
    assert ApplianceService(SqlDocuments(engine, "planning"), consumers).state == reopened.state
    previous, write = deepcopy(reopened.state), reopened.documents.write

    def fail_pointer(name, value):
        """Reject the final state pointer inside the transaction rather than after publication."""
        if name == "appliance-state.json":
            raise OSError("Fixture checkpoint outage")
        write(name, value)

    monkeypatch.setattr(reopened.documents, "write", fail_pointer)
    with pytest.raises(OSError):
        upload(reopened, [])
    assert reopened.state == previous
    assert ApplianceService(SqlDocuments(engine, "planning"), consumers).state == previous
    monkeypatch.setattr(reopened.documents, "write", write)
    upload(reopened, [])
    final = ApplianceService(SqlDocuments(engine, "planning"), consumers).document()["devices"][0]
    assert final["profile"]["evidence"] == "hourly_estimate"
    assert final["profile"]["samples"] == 2
    assert 1.9 < final["hourly_weight"] < 2
    assert final["history_import"]["expected_intervals"] == 192
    assert final["history_import"]["usable_intervals"] == 6
    assert history.request(final) is None


def test_archive_prefix_is_literal_and_namespace_bound(archive):
    """Percent and underscore in a prefix are literal characters on each SQL backend."""
    engine, _root = archive
    planning, demand = SqlDocuments(engine, "planning"), SqlDocuments(engine, "demand")
    for name, value in (("literal_%_one.json", 1), ("literal_XX_one.json", 2), ("other.json", 3)):
        planning.write(name, {"value": value})
    demand.write("literal_%_two.json", {"value": 4})
    assert list(planning.iter_prefix("literal_%_")) == [{"value": 1}]
    assert list(planning.iter_prefix("absent-")) == []
    with pytest.raises(RuntimeError, match="Fixture rollback"), planning.transaction():
        planning.write("literal_%_pending.json", {"value": 5})
        assert sorted(planning.iter_prefix("literal_%_"), key=lambda row: row["value"]) == [
            {"value": 1},
            {"value": 5},
        ]
        raise RuntimeError("Fixture rollback")
    assert list(planning.iter_prefix("literal_%_")) == [{"value": 1}]
    with pytest.raises(ValueError):
        list(planning.iter_prefix("../escape"))


def test_compressed_and_original_documents_share_all_read_paths(archive):
    """Each backend reopens compressed documents alongside unchanged original JSON using every read path."""
    engine, _root = archive
    original = {"intervals": [{"kwh": 0.30000000000000004, "source": "synthetic"}] * 300}
    documents = SqlDocuments(engine, "planning")
    documents.write("compression-new", original)
    with engine.begin() as connection:
        encoded = connection.scalar(
            select(DOCUMENTS.c.payload).where(
                DOCUMENTS.c.namespace == "planning", DOCUMENTS.c.name == "compression-new"
            )
        )
        assert encoded.startswith("oep:gzip:1:")
        assert len(encoded) < len(json.dumps(original)) / 4
        connection.execute(
            DOCUMENTS.insert().values(
                namespace="planning", name="compression-old", payload=json.dumps(original)
            )
        )
    reopened = SqlDocuments(engine, "planning")
    assert reopened.read("compression-new") == original
    assert reopened.read("compression-old") == original
    assert reopened.read_many(["compression-old", "compression-new"]) == {
        "compression-old": original,
        "compression-new": original,
    }
    assert list(reopened.iter_prefix("compression-")) == [original, original]


@pytest.fixture(params=BACKENDS, ids=[name for name, _ in BACKENDS])
def archive(request, tmp_path):
    """Own one disposable four-backend archive with every table initialized and no archive file writes."""
    backend, url = request.param
    engine = archive_engine(tmp_path) if backend == "sqlite" else create_engine(url)
    SCHEMA.drop_all(engine)
    repository = SqlPlanRepository(tmp_path, engine=engine)
    try:
        yield engine, tmp_path
    finally:
        repository.close()
        SCHEMA.drop_all(engine)
        engine.dispose()


def installation():
    """Persist Energy-owned charge links and multiple grid channels with reserve recovery on every backend."""
    return parse(
        {
            "grid_power_entity": "sensor.grid",
            "additional_grid_power_entities": ["sensor.grid2"],
            "grid_export_entity": "sensor.export",
            "additional_grid_export_entities": ["sensor.export2"],
            "import_limit_w": 5000,
            "export_limit_w": 5000,
            "batteries": [
                {
                    "device_id": "battery",
                    "energy_entity": "sensor.discharge",
                    "charge_energy_entity": "sensor.charge",
                    "inverter_id": "inv",
                    "soc_entity": "sensor.soc",
                    "power_entity": "sensor.power",
                    "capacity_kwh": 2,
                    "charge_limit_w": 1000,
                    "discharge_limit_w": 1000,
                    "recover_reserve": True,
                }
            ],
            "inverters": [{"inverter_id": "inv", "ac_limit_w": 1000, "efficiency": 1}],
        }
    )


def counters():
    """Retain two physical endpoints totaling 50 kWh through reopen and rollback on all four backends."""
    book = measured_inputs()[2]
    seed = replace(
        book.seeds[0],
        entity_id="sensor.combined_export",
        statistics=None,
        component_meters=(("sensor.export_one", 20), ("sensor.export_two", 30)),
    )
    return replace(book, seeds=(seed,))


CASES = (
    ("contracts", ContractStore, contract),
    ("bonus-counters", CounterStore, counters),
    ("demand", DemandStore, config),
    ("solar", SolarStore, plant),
    ("installation", InstallationStore, installation),
    ("prices", PriceStore, lambda: (price_series(),)),
)


def test_strategy_persists_on_active_backend(archive):
    """Every backend retains manual intent across reopen without a second database or runtime file."""
    engine, root = archive
    documents = SqlDocuments(engine, "planning")
    store = StrategyStore(documents)
    assert store.current == Strategy.COST
    store.save(Strategy.CONSUMPTION)
    restored = StrategyStore(SqlDocuments(engine, "planning"))
    assert restored.current == Strategy.CONSUMPTION
    assert restored.issue is None
    assert not (root / "planning").exists()


@pytest.mark.parametrize("namespace,store_type,value", CASES, ids=[row[0] for row in CASES])
def test_typed_store_reconstructs_from_database_only(archive, namespace, store_type, value):
    """All six durable stores preserve typed versions on reopen and never create runtime JSON archive files."""
    engine, root = archive
    document = value()
    store = store_type(SqlDocuments(engine, namespace))
    store.save(document)
    restored = store_type(SqlDocuments(engine, namespace))
    assert (restored.load() if namespace == "prices" else restored.current) == document
    assert not (root / namespace).exists()
    with engine.connect() as connection:
        assert (
            len(
                list(
                    connection.scalars(
                        select(DOCUMENTS.c.name).where(DOCUMENTS.c.namespace == namespace)
                    )
                )
            )
            == 2
        )


@pytest.mark.parametrize("namespace,store_type,value", CASES, ids=[row[0] for row in CASES])
def test_typed_store_rolls_back_version_and_pointer_together(archive, namespace, store_type, value):
    """An injected failure after the pointer INSERT rolls back both rows and leaves typed in-memory state unchanged."""
    engine, _root = archive
    store = store_type(SqlDocuments(engine, namespace))

    def fail(_connection, _cursor, statement, _parameters, context, _many):
        """Fail after a current-pointer row was actually inserted into the pending transaction."""
        if statement.lstrip().upper().startswith("INSERT INTO OEP_DOCUMENTS") and any(
            row.get("name") == "current.json" for row in context.compiled_parameters
        ):
            raise OSError("fixture pointer failure")

    event.listen(engine, "after_cursor_execute", fail)
    try:
        with pytest.raises(OSError, match="fixture pointer failure"):
            store.save(value())
    finally:
        event.remove(engine, "after_cursor_execute", fail)
    assert (store.load() if namespace == "prices" else store.current) in ((), None)
    with engine.connect() as connection:
        assert not connection.execute(
            select(DOCUMENTS.c.name).where(DOCUMENTS.c.namespace == namespace)
        ).first()


def test_unreleased_files_and_private_bootstrap_are_not_imported(tmp_path):
    """A fresh database ignores development JSON, secrets and logs. Test installations are configured afresh."""
    for namespace in DOCUMENT_NAMESPACES:
        folder = tmp_path / namespace
        folder.mkdir()
        (folder / "current.json").write_text("old-unreleased-content")
    for name in (
        "administration-token",
        "pairing.json",
        "certificate.pem",
        "database-options.json",
        "technical.log",
    ):
        (tmp_path / name).write_text("private-bootstrap-value")
    repository = SqlPlanRepository(tmp_path)
    try:
        with repository.engine.connect() as connection:
            assert not connection.execute(select(DOCUMENTS.c.name)).first()
        for namespace in ("pairing", "credentials", "logs"):
            with pytest.raises(ValueError, match="Unknown document namespace"):
                SqlDocuments(repository.engine, namespace)
        assert (tmp_path / "contracts/current.json").read_text() == "old-unreleased-content"
    finally:
        repository.close()


def test_corrupt_strategy_is_explicit_and_failed_save_preserves_intent(archive, monkeypatch):
    """Invalid persisted choice blocks readiness. Repairing it succeeds, while a failed next write retains that choice."""
    engine, _root = archive
    documents = SqlDocuments(engine, "planning")
    documents.write("current.json", "unknown")
    store = StrategyStore(documents)
    assert store.issue == "strategy_configuration_unreadable"
    store.save(Strategy.CONSUMPTION)
    assert store.issue is None

    def fail(*_args):
        """Simulate an unavailable write without mutating durable or live intent."""
        raise OSError("write fixture")

    monkeypatch.setattr(documents, "write", fail)
    with pytest.raises(OSError):
        store.save(Strategy.COST)
    assert store.current == Strategy.CONSUMPTION
    assert StrategyStore(SqlDocuments(engine, "planning")).current == Strategy.CONSUMPTION


@pytest.mark.parametrize("backend,url", BACKENDS, ids=[name for name, _ in BACKENDS])
def test_owned_engine_database_read_timeout(tmp_path, monkeypatch, backend, url):
    """External reads stop at driver-specific limits. SQLite retains its five-second lock bound."""
    monkeypatch.setattr(archive_adapter, "DATABASE_STATEMENT_TIMEOUT_MS", 100)
    monkeypatch.setattr(archive_adapter, "DATABASE_IO_TIMEOUT_SECONDS", 0.1)
    engine = archive_engine(tmp_path, url)
    try:
        if backend == "sqlite":
            with engine.connect() as connection:
                assert connection.scalar(text("PRAGMA busy_timeout")) == 5000
        else:
            with engine.connect() as connection:
                if backend == "postgresql":
                    assert connection.scalar(text("SHOW statement_timeout")) == "100ms"
                    assert connection.scalar(text("SHOW lock_timeout")) == "5s"
                    statement = "SELECT pg_sleep(1)"
                else:
                    statement = "SELECT SLEEP(1)"
                with pytest.raises(DBAPIError):
                    connection.execute(text(statement))
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()


def test_model_decision_evidence_roundtrip_and_atomic_index(archive, monkeypatch):
    """All four backends retain exact shadow evidence. A failed pointer update rolls back the new capture too."""
    from copy import deepcopy

    from open_energy_planner.forecast_selection.storage import SELECTION_INDEX, SelectionStore
    from open_energy_planner.solar.calculation.model import version

    engine, _root = archive
    documents = SqlDocuments(engine, "planning")
    store = SelectionStore(documents)
    capture = {
        "original_plan_id": "original",
        "start": "2026-10-22T00:00:00+00:00",
        "end": "2026-10-24T00:00:00+00:00",
        "fraction": 0.12345678912345678,
    }
    family = {"windows": [], "pending": version(capture)}
    store.commit("family", family, capture=capture)
    restored = SelectionStore(SqlDocuments(engine, "planning"))
    assert restored.state == store.state
    assert restored.read("capture", family["pending"]) == capture
    previous = deepcopy(store.state)
    changed = {**capture, "original_plan_id": "replacement"}
    original_write = documents.write

    def reject_pointer(name, value):
        """Fail after the immutable insert but before its current-index pointer can commit."""
        if name == SELECTION_INDEX:
            raise OSError("fixture pointer outage")
        original_write(name, value)

    monkeypatch.setattr(documents, "write", reject_pointer)
    with pytest.raises(OSError):
        store.commit("family", {"windows": [], "pending": version(changed)}, capture=changed)
    assert store.state == previous
    assert not documents.exists(f"model-capture-{version(changed)}.json")
    assert SelectionStore(SqlDocuments(engine, "planning")).state == previous


def test_individual_consumer_choices_share_the_active_database(archive):
    """All four backends retain versioned consumer choices in the existing demand document namespace."""
    from open_energy_planner.appliances.forecasting import ConsumerForecasts, parse_selection
    from tests.app.demand.test_consumers import catalogue

    engine, _root = archive
    consumers = ConsumerForecasts(SqlDocuments(engine, "demand"))
    consumers.sync(catalogue())
    selected = parse_selection([{"entity_id": "sensor.heat", "weather_sensitive": True}])
    consumers.save(selected, consumers.version)
    reopened = ConsumerForecasts(SqlDocuments(engine, "demand"))
    assert reopened.selected == selected
    assert reopened.result is None
    assert reopened.documents.read("consumer-settings.json") == list(selected)


def test_weather_learning_resumes_from_original_forward_evidence(archive):
    """Every backend reopens real native JSON and completes earlier unseen days before promoting."""
    from tests.app.forecast.support.weather_learning import forecast_day

    engine, _root = archive
    documents = SqlDocuments(engine, "demand")
    for day in (42, 43):
        forecast_day(documents, day)
    original = list(documents.iter_prefix("consumer-weather-window-"))
    reopened = SqlDocuments(engine, "demand")
    assert list(reopened.iter_prefix("consumer-weather-window-")) == original
    for day in (44, 45):
        model, _, evidence = forecast_day(reopened, day)
    assert model.model_id == "xgboost-weather-v1"
    assert evidence["completed_windows"] == 3
    assert all(
        window in list(reopened.iter_prefix("consumer-weather-window-")) for window in original
    )


def test_appliance_work_profiles_attempts_and_rollback_share_active_backend(archive, monkeypatch):
    """All four backends retain claimed/unknown work and cycle evidence. Failed state commit cannot advance live state."""
    from copy import deepcopy
    from datetime import timedelta

    from open_energy_planner.appliances.cycles.service import STATE_KEY, ApplianceService
    from tests.app.appliance.test_appliances import ENTITY, START, scheduled, service

    engine, _root = archive
    tasks = service(engine)
    draft = START + timedelta(days=1, hours=6)
    tasks.set_deadline_field(ENTITY, "date", "2026-10-25", "UTC", START + timedelta(days=1))
    tasks.set_deadline_field(ENTITY, "time", "06:00:00", "UTC", START + timedelta(days=1))
    current, plan, now = scheduled(tasks)
    tasks.claim(ENTITY, current["id"], current["attempt"]["id"], now, True, plan)
    tasks.tick(now + timedelta(minutes=3), True, plan)
    reopened = ApplianceService(SqlDocuments(engine, "planning"), tasks.consumers)
    assert reopened.state["tasks"][ENTITY]["status"] == "start_not_confirmed"
    assert reopened.state["devices"][ENTITY]["profile"]["samples"] == 1
    assert reopened.state["devices"][ENTITY]["finish_before"] == draft.isoformat()
    assert reopened.state["devices"][ENTITY]["finish_date"] == "2026-10-25"
    assert reopened.state["devices"][ENTITY]["finish_time"] == "06:00:00"
    previous = deepcopy(reopened.state)
    original_write = reopened.documents.write

    def fail_pointer(name, value):
        """Fail after version/task writes to prove rollback and unchanged in-memory authority."""
        if name == STATE_KEY:
            raise OSError("fixture outage")
        original_write(name, value)

    monkeypatch.setattr(reopened.documents, "write", fail_pointer)
    with pytest.raises(OSError):
        reopened.feedback(
            ENTITY,
            current["id"],
            current["attempt"]["id"],
            "not_started",
            now + timedelta(minutes=3),
            now + timedelta(minutes=3),
            cancelled=True,
        )
    assert reopened.state == previous
    assert ApplianceService(SqlDocuments(engine, "planning"), tasks.consumers).state == previous


def test_cancelled_historical_feedback_is_atomic_and_cannot_replace_current_work(
    archive, monkeypatch
):
    """All backends preserve late cancelled-task evidence atomically while replacement work and its revision remain unchanged."""
    from copy import deepcopy
    from datetime import timedelta

    from open_energy_planner.appliances.cycles.service import ApplianceService
    from tests.app.appliance.test_appliances import ENTITY, scheduled, service

    engine, _root = archive
    tasks = service(engine)
    old, _plan, now = scheduled(tasks)
    tasks.cancel_task(ENTITY, old["id"])
    tasks.submit(ENTITY, "new-work", now, now + timedelta(hours=6), now)
    tasks.feedback(ENTITY, old["id"], old["attempt"]["id"], "started", now, now)
    before, revision = deepcopy(tasks.state), tasks.revision
    canonical_key = "appliance-task-" + old["id"] + ".json"
    previous = tasks.documents.read(canonical_key)
    original_write = tasks.documents.write

    def fail_latest(key, value):
        """Fail the canonical write after the immutable feedback snapshot to prove transaction rollback."""
        if key == canonical_key:
            raise OSError("fixture archive outage")
        original_write(key, value)

    monkeypatch.setattr(tasks.documents, "write", fail_latest)
    end = now + timedelta(hours=2)
    with pytest.raises(OSError):
        tasks.feedback(ENTITY, old["id"], old["attempt"]["id"], "completed", end, end)
    assert tasks.state == before and tasks.revision == revision
    assert tasks.documents.read(canonical_key) == previous
    restored = ApplianceService(SqlDocuments(engine, "planning"), tasks.consumers)
    restored_before = deepcopy(restored.state)
    restored.feedback(ENTITY, old["id"], old["attempt"]["id"], "completed", end, end)
    assert restored.state == restored_before and restored.revision == revision
    assert restored.documents.read(canonical_key)["attempt"]["status"] == "completed"


def test_original_contract_start_roundtrips_without_rewriting_old_versions(archive):
    """All four backends retain the independent original contract date and still verify an older revision without that metadata."""
    engine, _root = archive
    documents = SqlDocuments(engine, "contracts")
    store = ContractStore(documents)
    original = contract()
    store.save(original)
    replacement = replace(
        original,
        periods=(
            replace(
                original.periods[0],
                contract_start=original.periods[0].effective_from - timedelta(days=200),
            ),
        ),
    )
    store.save(replacement)
    restored = ContractStore(SqlDocuments(engine, "contracts"))
    assert restored.current == replacement
    assert restored.current.periods[0].contract_start < restored.current.periods[0].effective_from
    assert restored.load(original.version) == original
    assert "contract_start" not in documents.read(original.version + ".json")["periods"][0]
    assert (
        documents.read(replacement.version + ".json")["periods"][0]["contract_start"]
        == replacement.periods[0].contract_start.isoformat()
    )


def test_bonus_origin_write_failure_keeps_book_and_pointer_atomic(archive, monkeypatch):
    """Failure writing explanatory provenance rolls back the new book on SQLite, PostgreSQL, MariaDB and MySQL."""
    from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
    from tests.app.history.support.bonus import NOW, START

    engine, _root = archive
    documents = SqlDocuments(engine, "bonus-counters")
    store = CounterStore(documents)
    first = CounterSeed("solar", START, 12.5, 5, "sensor.export", NOW)
    before = CounterBook(NOW, (first,))
    store.save(before)
    second = replace(first, used_kwh=13)
    after = CounterBook(NOW, (second,))
    write = documents.write

    def fail_origin(name, value):
        """Fail after the new immutable book was written but before its origin and pointer commit."""
        if name.endswith(".origin.json"):
            raise OSError("fixture provenance write failed")
        write(name, value)

    monkeypatch.setattr(documents, "write", fail_origin)
    with pytest.raises(OSError, match="provenance"):
        store.save_confirmation(after, seed=second)
    assert store.current == before
    assert not documents.exists(f"{after.version}.json")
    assert CounterStore(documents).current == before
    assert store.provenance(first) is None


@pytest.mark.parametrize("known_prefix", [False, True])
@pytest.mark.parametrize("negative_difference", [False, True])
def test_statistics_reference_prefix_and_rejected_correction_roundtrip(
    archive, known_prefix, negative_difference
):
    """All four databases retain sum references and rejected-history evidence across reopen."""
    from open_energy_planner.contracts.bonus_history import BonusHistory
    from tests.app.history.support.bonus import NOW, inputs
    from tests.app.history.test_bonus_statistics import derive, report

    engine, _root = archive
    documents = SqlDocuments(engine, "bonus-counters")
    store = CounterStore(documents)
    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, NOW)
    body = report(request, offset=0 if known_prefix else 2, state=False)
    if negative_difference:
        rows = body["statistics"][0]["hours"]
        rows[-1][1] = rows[0][1] - 1
    original = derive(body)
    store.save_statistics(original)
    seed = original.seeds[0]
    invalid = replace(
        original,
        seeds=(
            replace(
                seed,
                statistics=replace(
                    seed.statistics,
                    issue="bonus_statistics_revalidation_required",
                    rejected_digest="a" * 64,
                ),
            ),
        ),
    )
    store.save_statistics(invalid)
    restored = CounterStore(SqlDocuments(engine, "bonus-counters"))
    assert restored.current == invalid
    initial = restored.load(original.version).seeds[0]
    if known_prefix:
        assert initial.statistics.used(NOW) == (0 if negative_difference else 8)
    else:
        assert initial.used_kwh is None
        assert initial.meter_kwh is None
        assert initial.statistics.prefix_kwh is None
    assert restored.provenance(invalid.seeds[0])["supersedes"] == original.version
    with pytest.raises(ValueError, match="revalidation_required"):
        restored.current.seeds[0].statistics.used(NOW)


@pytest.mark.parametrize("kind", ["import", "bonus"])
def test_legacy_contract_with_undefined_added_example_remains_readable(archive, kind):
    """An archive valid at the original three prices survives restart when the added 0.20 sample is undefined."""
    engine, _ = archive
    body = settings()
    if kind == "import":
        body["periods"][0]["import_formula"] = "1 / (market - 0.2)"
    else:
        body["bonuses"] = [
            {
                "rule_id": "legacy-sample",
                "effective_from": body["periods"][0]["effective_from"],
                "rate_formula": "1 / abs(market - 0.2)",
                "cap_kwh": 100,
                "timezone": "UTC",
            }
        ]
    original = parse_contract(body, NOW, validate_examples=False)
    store = ContractStore(SqlDocuments(engine, "contracts"))
    store.save(original)
    restored = ContractStore(SqlDocuments(engine, "contracts"))
    assert restored.issue is None
    assert restored.current.version == original.version
    document = contract_document(restored.current)
    sample = (
        document["preview"][0][-1]
        if kind == "import"
        else document["bonus_preview"][0]["samples"][-1]
    )
    assert sample["market"] == 0.2
    assert sample["buy_price" if kind == "import" else "extra_rate"] is None
    assert sample["issue"] == "undefined_rate_example"
    with pytest.raises(ValueError, match="Undefined illustrative contract rate"):
        parse_contract(body, NOW)


def test_detected_appliance_profiles_restore_and_deduplicate_on_every_backend(archive):
    """All four engines retain detected evidence, manual fallback and deduplication after service reconstruction."""
    from open_energy_planner.appliances.cycles.service import ApplianceService
    from open_energy_planner.appliances.forecasting import ConsumerForecasts

    engine, _ = archive
    consumers = ConsumerForecasts(SqlDocuments(engine, "demand"))
    consumers.sync(
        {
            "entities": [{"entity_id": ENTITY, "name": "Washer"}],
            "suggestions": {
                "consumers": [{"entity_id": ENTITY, "power_entity": POWER, "upstream": None}]
            },
        }
    )
    consumers.save([{"entity_id": ENTITY, "weather_sensitive": False}], consumers.version)
    documents = SqlDocuments(engine, "planning")
    service = ApplianceService(documents, consumers)
    service.set_manual_profile(ENTITY, 90, 0.6, service.revision)
    service.learn_history(ENTITY, POWER, readings(), False, LEARNING_NOW)
    restored = ApplianceService(documents, consumers)
    restored.learn_history(ENTITY, POWER, readings(), False, LEARNING_NOW)
    row = restored.document()["devices"][0]
    assert row["profile_kind"] == "Learned" and row["profile"]["samples"] == 2
    assert row["manual_profile"] == {"duration_minutes": 90, "energy_kwh": 0.6}
    assert [cycle["energy_kwh"] for cycle in row["recent_cycles"]] == pytest.approx([0.5, 0.5])


def test_calculation_history_survives_restart_and_selects_dst_day(archive):
    """Both 02:30 occurrences belong to Amsterdam's 25-hour day, with no next-day leakage."""
    from datetime import UTC, datetime

    from open_energy_planner.optimization.domain import SolveResult, SolveSample, Status
    from open_energy_planner.optimization.performance import CalculationHistory
    from open_energy_planner.planning import PlanningOutcome
    from open_energy_planner.synthetic import capture

    engine, _ = archive
    store = CalculationHistory(SqlDocuments(engine, "planning"))
    snapshot = capture(datetime(2026, 10, 25, tzinfo=UTC))
    result = SolveResult(
        Status.OPTIMAL,
        None,
        "optimal",
        1,
        3,
        -1,
        progress=(SolveSample(2, 0, -1), SolveSample(4, -0.5, -1)),
        first_candidate_seconds=2,
        time_limit_seconds=120,
    )
    for hour in (0, 1, 23):
        started = datetime(2026, 10, 25, hour, 30, tzinfo=UTC)
        store.record(
            snapshot,
            PlanningOutcome(None, "candidate_rejected", result),
            started,
            started + timedelta(seconds=4),
            "candidate_rejected",
        )
    restored = CalculationHistory(SqlDocuments(engine, "planning"))
    rows = restored.day("2026-10-25", "Europe/Amsterdam")
    assert len(rows) == 2
    assert [row["started_at"] for row in rows] == [
        "2026-10-25T00:30:00+00:00",
        "2026-10-25T01:30:00+00:00",
    ]
    assert "progress" not in rows[0]
    assert restored.detail(rows[0]["id"])["progress"][0]["objective"] == 0
    assert rows[0]["first_candidate_seconds"] == 2
    assert rows[0]["plan_id"] is None
    assert len(restored.day("2026-10-26", "Europe/Amsterdam")) == 1
    with pytest.raises(ValueError):
        restored.day("2026-02-30", "Europe/Amsterdam")


def test_calculation_index_and_progress_rollback_as_one_write(archive, monkeypatch):
    """A failed daily index cannot leave orphan progress, and daily reads never load detail curves."""
    from datetime import UTC, datetime

    from open_energy_planner.optimization.performance import CalculationHistory
    from open_energy_planner.synthetic import capture

    engine, _ = archive
    documents = SqlDocuments(engine, "planning")
    history = CalculationHistory(documents)
    started = datetime(2026, 10, 25, tzinfo=UTC)
    write = documents.write

    def fail_index(name, value):
        """Reject the second write after progress was inserted within the shared transaction."""
        if name.startswith("calculations-"):
            raise OSError("Simulated daily index failure")
        write(name, value)

    monkeypatch.setattr(documents, "write", fail_index)
    with pytest.raises(OSError):
        history.record(capture(started), None, started, started, "planning_error")
    assert list(documents.iter_prefix("calculation-")) == []
    assert history.day("2026-10-25", "Europe/Amsterdam") == []
    monkeypatch.setattr(documents, "write", write)
    history.record(capture(started), None, started, started, "planning_error")
    reads, read = [], documents.read

    def observe_read(name):
        """Track actual database document reads instead of inspecting implementation source."""
        reads.append(name)
        return read(name)

    monkeypatch.setattr(documents, "read", observe_read)
    assert len(history.day("2026-10-25", "Europe/Amsterdam")) == 1
    assert not any(name.startswith("calculation-") for name in reads)


def test_solar_nonlinear_weights_and_training_reports_survive_backend_restart(archive):
    """Every real backend retains source-bound native weights, immutable cleaning evidence and baseline selection."""
    from open_energy_planner.solar.boosting import SolarBoostedModel
    from tests.app.solar.test_boosting import runtime_case

    engine, root = archive
    runtime, now = runtime_case(root, store=SolarStore(SqlDocuments(engine, "solar")))
    assert runtime.install(runtime.build(runtime.job(now)), now)
    reference = runtime.learning["pv0"].archive_reference
    restored = SolarStore(SqlDocuments(engine, "solar"))
    report = restored.documents.read("ml-training-" + reference + ".json")
    model = report["model"]
    weights = restored.documents.read("ml-weights-" + model["weights_reference"] + ".json")
    replay = SolarBoostedModel.restore(
        {
            **{key: value for key, value in model.items() if key != "weights_reference"},
            "model_json": weights["model_json"],
        }
    )
    assert replay == runtime.learning["pv0"].model
    assert report["summary"]["status"] == "nonlinear_shadow"
    assert report["summary"]["validation_hours"] >= 28
    assert restored.current == runtime.configuration
    choice = restored.documents.read("source-choice-" + runtime.forecast.version + ".json")
    assert choice["sources"]["pv0"]["active"] == "pvwatts"
    assert restored.documents.read("ml-training-" + reference + ".json") == report


def test_solar_learning_boundary_and_time_trend_replay_after_backend_restart(archive):
    """Each real backend preserves the explicit learning regime and continuous measured-yield correction."""
    from open_energy_planner.solar.boosting import SolarBoostedModel
    from open_energy_planner.solar.trend import YieldTrend
    from open_energy_planner.solar.wire import document
    from tests.app.solar.support import SITE
    from tests.app.solar.test_boosting import fit_case

    engine, _root = archive
    result, _history, _physical, weather, now = fit_case()
    model = replace(result.model, time_trend=YieldTrend(-0.005, now - timedelta(days=30)))
    store = SolarStore(SqlDocuments(engine, "solar"))
    configuration = plant()
    configuration = replace(
        configuration,
        sources=(
            replace(
                configuration.sources[0], learning_start=(now - timedelta(days=40)).isoformat()
            ),
        ),
    )
    store.save(configuration)
    store.archive("ml-training", model.version, document(model))
    restored = SolarStore(SqlDocuments(engine, "solar"))
    replay = SolarBoostedModel.restore(
        restored.documents.read("ml-training-" + model.version + ".json")
    )
    assert restored.current == configuration
    assert replay == model
    assert replay.predict(weather[-24:], SITE, known_at=now) == model.predict(
        weather[-24:], SITE, known_at=now
    )


def test_shared_solar_writers_preserve_one_immutable_content_and_release_transactions(archive):
    """Concurrent model and forecast-cache writers safely reuse identical content on every backend."""
    from concurrent.futures import ThreadPoolExecutor

    engine, _root = archive
    store = SolarStore(SqlDocuments(engine, "solar"))
    payload = {"synthetic": True, "values": [0.0, 1.5, 3.0]}

    def write_same_content(_):
        """Exercise the public immutable write path from independent worker threads."""
        for _ in range(5):
            store.archive("concurrent-proof", "same", payload)

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(write_same_content, range(8)))
    restored = SolarStore(SqlDocuments(engine, "solar"))
    assert restored.documents.read("concurrent-proof-same.json") == payload
    with pytest.raises(ValueError, match="Solar evidence archive mismatch"):
        store.archive("concurrent-proof", "same", {"synthetic": False})
    store.archive("other-source", "after-conflict", {"retained": True})
    assert restored.documents.read("other-source-after-conflict.json") == {"retained": True}


def test_historical_forecast_assessment_and_original_run_cache_survive_restart(archive):
    """All backends retain both original forecast receipts and derived admission without new downloads."""
    from open_energy_planner.solar.backtest import HistoricalForecastAssessment
    from open_energy_planner.solar.storage.forecast_weather import ForecastWeatherCache
    from open_energy_planner.solar.weather import forecast_weather_query
    from open_energy_planner.solar.wire import document
    from tests.database.support.solar import forecast_evidence

    engine, _root = archive
    site, run, report = forecast_evidence()
    store = SolarStore(SqlDocuments(engine, "solar"))
    store.archive("historical-assessment", "synthetic", document(report))
    query = forecast_weather_query(site, run.initialized_at)
    ForecastWeatherCache(store).write(query, run)
    restored = SolarStore(SqlDocuments(engine, "solar"))
    replay = HistoricalForecastAssessment.restore(
        restored.documents.read("historical-assessment-synthetic.json")
    )
    assert (
        replay.status == "eligible" and replay.matched_pairs == 384 and replay.unique_hours == 288
    )
    assert replay == report
    assert ForecastWeatherCache(restored).read(query) == run


def solar_series_rows():
    """Use three distinct daily raw evidence blocks without claiming complete learning coverage."""
    return [
        {
            "start": f"2026-01-0{index + 1}T00:00:00+00:00",
            "total_kwh": float(index),
            "meter_kwh": float(index),
        }
        for index in range(3)
    ]


def solar_documents_snapshot(engine):
    """Detach every solar payload to detect overwritten evidence or stray partial inserts."""
    with engine.connect() as connection:
        return dict(
            connection.execute(
                select(DOCUMENTS.c.name, DOCUMENTS.c.payload).where(
                    DOCUMENTS.c.namespace == "solar"
                )
            ).all()
        )


@pytest.mark.parametrize("damage", ["manifest", "block", "missing", "input"])
def test_existing_solar_series_rejects_conflicting_or_incomplete_evidence(archive, damage):
    """Manifest presence cannot hide corrupt, missing or differently supplied immutable blocks."""
    engine, _root = archive
    store = SolarStore(SqlDocuments(engine, "solar"))
    rows = solar_series_rows()
    store.archive_series("history", "synthetic-series", rows, entity="sensor.synthetic")
    manifest = store.documents.read("history-synthetic-series.json")
    block = "block-" + manifest["blocks"][1]["block"] + ".json"
    if damage == "manifest":
        store.documents.write("history-synthetic-series.json", {"entity_id": "other"})
    elif damage == "block":
        store.documents.write(block, {"entity_id": "sensor.synthetic", "hours": []})
    elif damage == "missing":
        with engine.begin() as connection:
            connection.execute(
                DOCUMENTS.delete().where(
                    DOCUMENTS.c.namespace == "solar", DOCUMENTS.c.name == block
                )
            )
    else:
        rows[1] = {**rows[1], "total_kwh": 99.0}
    before = solar_documents_snapshot(engine)
    restored = SolarStore(SqlDocuments(engine, "solar"))
    with pytest.raises(ValueError, match="Solar evidence archive mismatch"):
        restored.archive_series("history", "synthetic-series", rows, entity="sensor.synthetic")
    assert solar_documents_snapshot(engine) == before


def test_solar_series_finishes_prior_partial_blocks_and_rolls_back_failed_manifest(
    archive, monkeypatch
):
    """A failed new publication adds no blocks, while an accepted legacy partial block remains recoverable."""
    from open_energy_planner.solar.calculation.model import version

    engine, _root = archive
    store = SolarStore(SqlDocuments(engine, "solar"))
    rows = solar_series_rows()
    body = {"entity_id": "sensor.synthetic", "hours": rows[:1]}
    store.archive("block", version(body), body)
    before = solar_documents_snapshot(engine)
    original_write = store.documents.write

    def failed_manifest(name, payload):
        """Fail after the new blocks are staged but before the public series manifest commits."""
        if name == "history-synthetic-series.json":
            raise OSError("Synthetic manifest write failure")
        return original_write(name, payload)

    monkeypatch.setattr(store.documents, "write", failed_manifest)
    with pytest.raises(OSError, match="Synthetic manifest write failure"):
        store.archive_series("history", "synthetic-series", rows, entity="sensor.synthetic")
    assert solar_documents_snapshot(engine) == before
    monkeypatch.setattr(store.documents, "write", original_write)
    store.archive_series("history", "synthetic-series", rows, entity="sensor.synthetic")
    restored = SolarStore(SqlDocuments(engine, "solar"))
    manifest = restored.documents.read("history-synthetic-series.json")
    replay = [
        row
        for item in manifest["blocks"]
        for row in restored.documents.read("block-" + item["block"] + ".json")["hours"]
    ]
    assert replay == rows


def test_concurrent_solar_series_writers_publish_one_complete_manifest(archive):
    """Independent model/cache threads atomically reuse identical complete series evidence."""
    from concurrent.futures import ThreadPoolExecutor

    engine, _root = archive
    store = SolarStore(SqlDocuments(engine, "solar"))
    rows = solar_series_rows()

    def write(_index):
        """Publish the same original evidence through the actual shared domain writer lock."""
        store.archive_series("history", "synthetic-series", rows, entity="sensor.synthetic")

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(write, range(8)))
    restored = SolarStore(SqlDocuments(engine, "solar"))
    manifest = restored.documents.read("history-synthetic-series.json")
    assert len(manifest["blocks"]) == 3
    assert all(
        restored.documents.read("block-" + item["block"] + ".json")["hours"] == [row]
        for item, row in zip(manifest["blocks"], rows, strict=True)
    )
