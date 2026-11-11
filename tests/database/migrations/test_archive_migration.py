"""Cross-backend maintenance preserves source history and activates only a verified replacement."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, func, select

from open_energy_planner.prices.selection import ForecastSelection
from open_energy_planner.prices.storage.adapters.selection_store import SqlAlchemySelectionStore
from open_energy_planner.storage.adapters.documents import DOCUMENT_NAMESPACES, SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.migration import migrate_archive
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SCHEMA,
    SqlPlanRepository,
    archive_engine,
)
from tests.database.contracts.test_plan_archive_backends import plan
from tests.database.contracts.test_selection_store_backends import NOW, evidence
from tests.database.support.backends import BACKENDS

URLS = dict(BACKENDS)
ROUTES = [
    (left, right)
    for left, _ in BACKENDS
    for right, _ in BACKENDS
    if left != right or left == "sqlite"
]


def test_battery_calibration_transfers_without_source_dependency(route):
    """Every backend route preserves calibration after the original database is closed."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from open_energy_planner.installation.model import Installation, InstalledBattery
    from open_energy_planner.installation.storage import InstallationStore
    from open_energy_planner.solar.calculation.model import SolarInverter

    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    store = InstallationStore(SqlDocuments(source, "installation"))
    battery = InstalledBattery(
        "battery",
        "inv",
        "sensor.soc",
        "sensor.power",
        2,
        4000,
        4000,
        calibration_enabled=True,
        calibration_hold_hours=0.25,
    )
    settings = Installation("sensor.grid", 4000, 4000, (SolarInverter("inv", 4000, 1),), (battery,))
    store.save(settings)
    start = datetime.now(UTC)
    for minute in (0, 5, 10, 15):
        store.observe(
            SimpleNamespace(
                captured_at=start + timedelta(minutes=minute),
                readings=tuple(
                    SimpleNamespace(
                        key=f"battery.battery.{key}", entity_id=entity, value=value, unit=unit
                    )
                    for key, entity, value, unit in (
                        ("soc", "sensor.soc", 100 if minute == 0 else 98, "%"),
                        ("power", "sensor.power", 0, "W"),
                    )
                ),
            )
        )
    before = deepcopy(store.calibration.state)
    assert before["battery"]["completed_at"] is not None
    migrate_archive(source, target, overwrite_target=True)
    original.close()
    source.dispose()
    restored = InstallationStore(SqlDocuments(target, "installation"))
    assert restored.current == settings
    assert restored.calibration.state == before
    assert restored.calibration.due_at(battery) == start + timedelta(minutes=15, days=14)


def test_solar_feedback_transfers_without_its_original_database(route):
    """All backend routes preserve sensor binding, first observation, unknown state and immutable feedback references."""
    from open_energy_planner.solar.curtailment import (
        CurtailmentHistory,
        archive_feedback,
        load_feedback,
        save_feedback,
    )
    from open_energy_planner.solar.storage import SolarStore
    from tests.app.solar.support import START, plant

    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    store = SolarStore(SqlDocuments(source, "solar"))
    configuration = replace(
        plant(),
        sources=(replace(plant().sources[0], curtailment_entity_id="binary_sensor.curtailed"),),
    )
    store.save(configuration)
    feedback = CurtailmentHistory(
        "binary_sensor.curtailed",
        START + timedelta(hours=3),
        ((START, False), (START + timedelta(hours=1), True), (START + timedelta(hours=2), None)),
    )
    accepted = save_feedback(store, None, feedback, START - timedelta(hours=1))
    reference = archive_feedback(store, accepted)

    def feedback_documents(documents):
        """Compare complete feedback payloads without relying on database row order."""
        return sorted(
            json.dumps(row, sort_keys=True) for row in documents.iter_prefix("curtailment-")
        )

    original_documents = feedback_documents(store.documents)
    migrate_archive(source, target, overwrite_target=True)
    original.close()
    source.dispose()
    restored = SolarStore(SqlDocuments(target, "solar"))
    assert restored.current == configuration
    assert feedback_documents(restored.documents) == original_documents
    accepted = load_feedback(
        restored.documents, "binary_sensor.curtailed", START + timedelta(hours=4)
    )
    assert accepted == feedback
    assert (
        load_feedback(
            restored.documents,
            "binary_sensor.curtailed",
            START + timedelta(hours=4),
            reference=reference,
        )
        == feedback
    )
    assert accepted.unrestricted(START, START + timedelta(hours=1))
    assert not accepted.unrestricted(START + timedelta(hours=2), START + timedelta(hours=3))


def test_annual_demand_cohort_transfers_and_restarts_without_source(route, monkeypatch):
    """Every real backend route retains all annual source evidence and the exact accepted cohort independently."""
    from contextlib import closing
    from datetime import UTC, datetime

    from open_energy_planner.demand.reconstruction import DemandReconstructor
    from open_energy_planner.demand.runtime import DemandRuntime
    from open_energy_planner.demand.storage import DemandStore
    from open_energy_planner.history.importer import CumulativeHistory, EnergyEndpoint
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from tests.app.demand.history_support import receive_source
    from tests.app.demand.test_demand import config

    (source, target), root = route
    now = datetime(2026, 6, 1, tzinfo=UTC)
    history = CumulativeHistory(
        "sensor.house",
        tuple(
            EnergyEndpoint(datetime(year, 1, 1, hour, tzinfo=UTC), (year - 2020) * 100 + hour)
            for year in (2024, 2025, 2026)
            for hour in (0, 1)
        ),
    )
    with closing(SqlPlanRepository(root / "source", engine=source)):
        store = DemandStore(SqlDocuments(source, "demand"))
        store.save(config())
        runtime = DemandRuntime(store)
        receive_source(runtime, history, now)
        pointer = "household-history-cohort-" + store.current.version + ".json"
        references = store.documents.read(pointer)
        migrate_archive(source, target, overwrite_target=True)

        def unavailable(*_args, **_kwargs):
            """Fail if target restoration accidentally consults its former source engine."""
            raise AssertionError("Source engine must remain disconnected")

        with monkeypatch.context() as disconnected:
            disconnected.setattr(source, "connect", unavailable)
            disconnected.setattr(source, "begin", unavailable)
            transferred = DemandRuntime(DemandStore(SqlDocuments(target, "demand")))
            assert transferred.histories == {"sensor.house": history}
            assert transferred.store.documents.read(pointer) == references
            assert transferred.history_import.restore_checkpoint(references) == {
                "sensor.house": history
            }
            assert transferred.request(now) is None
            samples = DemandReconstructor().reconstruct(
                transferred.configuration, transferred.histories.values(), before=now
            )
            assert [(sample.start, sample.residual_kwh) for sample in samples] == [
                (datetime(year, 1, 1, 1, tzinfo=UTC), 1) for year in (2024, 2025, 2026)
            ]
            assert transferred.history_import.status(("sensor.house",), transferred.histories)[
                "sensor.house"
            ]["loaded_years"] == [2024, 2025, 2026]


def test_transfer_progress_reports_real_copy_and_verification_counts(route):
    """Every backend route reports monotonic bounded counts for both phases and preserves exact plans."""
    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    original.state["plan"] = plan("progress-proof")
    original.save()
    progress = []
    counts = migrate_archive(
        source,
        target,
        overwrite_target=True,
        progress=lambda phase, done, total: progress.append((phase, done, total)),
    )
    for phase in ("copying", "verifying"):
        rows = [entry for entry in progress if entry[0] == phase]
        assert rows[0][1] == 0
        assert rows[-1][1] == rows[-1][2] == sum(counts.values())
        assert [entry[1] for entry in rows] == sorted(entry[1] for entry in rows)
        assert all(0 <= done <= total for _, done, total in rows)
    restored = SqlPlanRepository(root / "target", engine=target)
    assert restored.state == original.state


def test_final_daily_results_and_controls_survive_every_transfer_route_without_source(route):
    """A sealed partial day's .60 cost and -.20 benefit remain usable after full plans, raw quarters and source schema disappear."""
    from dataclasses import asdict

    from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
    from open_energy_planner.prices.storage.adapters.published import retain
    from open_energy_planner.savings.daily import DailySavings
    from open_energy_planner.savings.runtime import SavingsRuntime
    from open_energy_planner.savings.storage.adapters.sqlalchemy import SettlementStore, stamp
    from open_energy_planner.storage.adapters.lifecycle.retention import collect_blocks, thin
    from open_energy_planner.storage.adapters.lifecycle.schema import (
        ACTIVATIONS,
        PLAN_CONTEXTS,
        PLANS,
    )
    from tests.app.savings.support.scenarios import END, START, bindings, contract, frame, problem

    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    identity = "daily-transfer-original"
    original.state["plan"] = {
        "plan_id": identity,
        "generated_at": START.isoformat(),
        "strategy": "minimize cost",
        "solution_status": "optimal",
        "forecast_quality_controlled": ["sensor.synthetic_dishwasher"],
        "input_snapshot": {
            "problem": json.loads(
                json.dumps(asdict(problem()), default=lambda value: value.isoformat())
            )
        },
        "steps": [],
    }
    original.save()
    store, documents = SettlementStore(source), SqlDocuments(source, "savings")
    with source.begin() as connection:
        connection.execute(ACTIVATIONS.insert().values(activated_at=stamp(START), plan_id=identity))
        retain(
            connection,
            (
                PriceSeries(
                    "published",
                    "NL",
                    "EUR",
                    START - timedelta(hours=1),
                    None,
                    None,
                    tuple(
                        PricePoint(
                            START + timedelta(minutes=index * 15),
                            START + timedelta(minutes=(index + 1) * 15),
                            0.1,
                            PriceKind.PUBLISHED,
                            15,
                        )
                        for index in range(2)
                    ),
                ),
            ),
        )
    store.put_frames(
        (frame(), frame(start=END)), {"bindings": bindings(), "seeds": [], "location": None}
    )
    runtime = SavingsRuntime(store, documents, original)
    report = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", contract(), None, END + timedelta(days=4), START, None
    )
    report["finalized_at"] = (END + timedelta(days=4)).isoformat()
    store.put_day(report, documents, "2026-10-23")
    thin(original, documents, END + timedelta(days=5), "UTC")
    assert store.frames() == []
    original.preserve_history()
    original.state["plan"] = None
    original.save()
    with source.begin() as connection:
        connection.execute(
            ACTIVATIONS.update().where(ACTIVATIONS.c.plan_id == identity).values(plan_id=None)
        )
        connection.execute(
            PLAN_CONTEXTS.update().where(PLAN_CONTEXTS.c.plan_id == identity).values(payload=None)
        )
        connection.execute(PLANS.delete().where(PLANS.c.plan_id == identity))
        collect_blocks(connection)
    migrate_archive(source, target, overwrite_target=True)
    SCHEMA.drop_all(source)
    restored_plans = SqlPlanRepository(root / "target", engine=target)
    restored_store = SettlementStore(target)
    assert restored_store.day(finalized=True) == report
    assert restored_plans.get_plan(identity) is None
    assert restored_plans.get_context(identity) is None
    assert restored_store.context_plan(START) == identity
    assert restored_plans.controlled_entities([identity]) == ["sensor.synthetic_dishwasher"]
    restored = DailySavings(
        SavingsRuntime(restored_store, SqlDocuments(target, "savings"), restored_plans)
    )
    result = restored.overview(START.replace(hour=0), START.replace(hour=0) + timedelta(days=1))
    assert result["totals"]["actual"]["purchase"] == pytest.approx(0.6)
    assert result["savings"] == pytest.approx(-0.2)
    assert result["days"][0]["scenarios"]["actual"]["status"] == "partial"


def test_weather_learning_transfers_and_finishes_without_source(route):
    """Every backend transfer retains native model JSON and forward evidence after the source closes."""
    from tests.app.forecast.support.weather_learning import forecast_day

    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    SqlPlanRepository(root / "target", engine=target)
    documents = SqlDocuments(source, "demand")
    forecast_day(documents, 42)
    forecast_day(documents, 43)
    preserved = list(documents.iter_prefix("consumer-weather-"))
    migrate_archive(source, target, overwrite_target=True)
    original.close()
    source.dispose()
    restored = SqlDocuments(target, "demand")
    assert list(restored.iter_prefix("consumer-weather-")) == preserved
    for day in (44, 45):
        model, _, evidence = forecast_day(restored, day)
    assert model.model_id == "xgboost-weather-v1"
    assert evidence["completed_windows"] == 3


def test_daily_demand_selection_transfers_and_continues_without_source(route):
    """Every backend transfer preserves frozen days and resumes model selection from the target alone."""
    from open_energy_planner.forecast_selection.accuracy import choose_daily
    from tests.app.forecast.support.daily_accuracy import START, forecasts, measurements

    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    SqlPlanRepository(root / "target", engine=target)
    documents = SqlDocuments(source, "demand")
    for offset in range(4):
        now = START + timedelta(days=offset)
        choose_daily(documents, "washer", "UTC", forecasts(now), measurements(now), now)
    preserved = list(documents.iter_prefix("accuracy-"))
    assert preserved
    migrate_archive(source, target, overwrite_target=True)
    original.close()
    source.dispose()
    restored = SqlDocuments(target, "demand")
    assert list(restored.iter_prefix("accuracy-")) == preserved
    for offset in (4, 5):
        now = START + timedelta(days=offset)
        result = choose_daily(restored, "washer", "UTC", forecasts(now), measurements(now), now)
    assert result["selected"] == "candidate"
    assert result["completed_windows"] == 3


def test_data_storage_migration_restarts_real_app_on_verified_target(route):
    """Real managed HTTP migration preserves history across every backend route and rejects local self-copy."""
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.app import create_app
    from open_energy_planner.runtime.maintenance import MAINTENANCE, create_managed_app
    from open_energy_planner.storage.adapters.lifecycle.maintenance import write_private

    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    original.state["plan"] = plan("ui-migration-proof")
    original.save()
    source_url = (
        source.url.render_as_string(hide_password=False)
        if source.dialect.name != "sqlite"
        else None
    )
    target_url = (
        target.url.render_as_string(hide_password=False)
        if target.dialect.name != "sqlite"
        else None
    )
    bootstrap = root / ("source" if source_url is None else "target")
    bootstrap.mkdir(exist_ok=True)
    write_private(bootstrap, "database-selection.json", {"url": source_url})

    async def scenario():
        """Use actual authentication, task drain, copying, startup validation and public admission."""
        token = "ui-migration-test-private-token-123456789"
        headers = {"Authorization": f"Bearer {token}"}
        app = create_managed_app(bootstrap, token, factory=create_app, source="home_assistant")
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/storage/migration/check", headers=headers, json={"url": target_url}
            )
            if source_url is None and target_url is None:
                assert response.status == 400
                assert original.get_plan("ui-migration-proof") is not None
                return
            assert response.status == 200
            evidence = await response.json()
            response = await client.post(
                "/v1/storage/migration/start",
                headers=headers,
                json={"check_id": evidence["check_id"], "overwrite": True},
            )
            assert response.status == 202
            await asyncio.wait_for(app[MAINTENANCE].task, timeout=30)
            status = await (await client.get("/v1/storage/migration", headers=headers)).json()
            assert status["phase"] == "completed" and status["released"] and status["running"]
            response = await client.get("/v1/plans/ui-migration-proof", headers=headers)
            assert response.status == 200
            assert (await response.json())["plan_id"] == "ui-migration-proof"
            assert original.get_plan("ui-migration-proof") == plan("ui-migration-proof")
        SCHEMA.drop_all(source)
        restarted = create_managed_app(
            bootstrap, token, factory=create_app, source="home_assistant"
        )
        async with TestClient(TestServer(restarted)) as client:
            response = await client.get("/v1/plans/ui-migration-proof", headers=headers)
            assert response.status == 200
            status = await (await client.get("/v1/storage/migration", headers=headers)).json()
            assert status["running"] and status["phase"] == "completed"

    asyncio.run(scenario())


def test_candidate_startup_rejects_missing_state_without_creating_new_history(route):
    """Every destination backend fails startup validation rather than repairing lost state with a new identity."""
    from sqlalchemy import select

    from open_energy_planner.storage.adapters.lifecycle.maintenance import check_candidate
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import STATE

    (_, target), root = route
    SqlPlanRepository(root / "target", engine=target)
    with target.begin() as connection:
        connection.execute(STATE.delete())
    target_url = (
        target.url.render_as_string(hide_password=False)
        if target.dialect.name != "sqlite"
        else None
    )
    with pytest.raises(ValueError, match="state"):
        check_candidate(root / "target", target_url)
    with target.connect() as connection:
        assert connection.scalar(select(STATE.c.id)) is None


def test_hourly_learning_resumes_after_cross_backend_transfer_without_source(route):
    """Every supported transfer preserves the actual hourly model, source-bound checkpoint and original cycle points."""
    from open_energy_planner.appliances.cycles.service import ApplianceService
    from open_energy_planner.appliances.forecasting import ConsumerForecasts
    from tests.app.appliance.test_appliance_learning import ENTITY, NOW, POWER
    from tests.app.appliance.test_history_learning import begin, hourly_rows, upload

    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    SqlPlanRepository(root / "target", engine=target)
    consumers = ConsumerForecasts(SqlDocuments(source, "demand"))
    consumers.sync(
        {
            "entities": [{"entity_id": ENTITY, "name": "Washer"}],
            "suggestions": {
                "consumers": [{"entity_id": ENTITY, "upstream": None, "power_entity": POWER}]
            },
        }
    )
    consumers.save([{"entity_id": ENTITY, "weather_sensitive": False}], consumers.version)
    tasks = ApplianceService(SqlDocuments(source, "planning"), consumers)
    start = NOW - timedelta(days=700)
    begin(tasks, start, start + timedelta(days=8))
    upload(tasks, hourly_rows(start, [0, 0, 500, 0, 0, 1000, 0]))
    before = json.loads(json.dumps(tasks.state))
    archived = list(tasks.documents.iter_prefix("appliance-hourly-"))
    progress = tasks.documents.read("appliance-progress.json")
    migrate_archive(source, target, overwrite_target=True)
    original.close()
    source.dispose()
    restored_consumers = ConsumerForecasts(SqlDocuments(target, "demand"))
    restored_consumers.options = consumers.options
    restored = ApplianceService(SqlDocuments(target, "planning"), restored_consumers)
    assert restored.state == before
    assert restored.documents.read("appliance-progress.json") == progress
    assert sorted(
        restored.documents.iter_prefix("appliance-hourly-"), key=lambda row: row["id"]
    ) == sorted(archived, key=lambda row: row["id"])
    upload(restored, [])
    row = restored.document()["devices"][0]
    assert row["profile"]["samples"] == 2
    assert row["profile"]["evidence"] == "hourly_estimate"
    assert row["history_import"]["phase"] == "complete"


@pytest.fixture(params=ROUTES, ids=[f"{left}-to-{right}" for left, right in ROUTES])
def route(request, tmp_path):
    """Create two independent disposable archives, including all ordered cross-backend pairs in CI."""
    left, right = request.param
    engines = [
        archive_engine(tmp_path / name, URLS[backend])
        for name, backend in (("source", left), ("target", right))
    ]
    for engine in engines:
        SCHEMA.drop_all(engine)
    try:
        yield engines, tmp_path
    finally:
        for engine in engines:
            SCHEMA.drop_all(engine)
            engine.dispose()


def test_migration_rebuilds_target_and_preserves_exact_source(route, monkeypatch):
    """Every supported route replaces target history, retains pause/identity/state and preserves all original plans exactly."""
    from open_energy_planner.storage.adapters.lifecycle import migration

    monkeypatch.setattr(migration, "MIGRATION_BATCH_ROWS", 1)
    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    first = plan("a")
    second = plan("B", minute="15", changed_measurement=True)
    for document in (first, second):
        original.state["plan"] = document
        original.save()
    original.state["enabled"] = False
    original.state["measurement_config"] = {"revision": "retained-bindings"}
    original.save()
    state = deepcopy(original.state)
    selection = SqlAlchemySelectionStore(source)
    selector = ForecastSelection(selection, "NL", "primary", "alternative")
    pairs, actuals = evidence()
    selection.update(
        "NL", pairs, (), NOW - timedelta(days=8), NOW - timedelta(days=38), selector.decide
    )
    expected_selection = selection.update(
        "NL", (), actuals, NOW, NOW - timedelta(days=30), selector.decide
    )
    for namespace in DOCUMENT_NAMESPACES:
        documents = SqlDocuments(source, namespace)
        with documents.transaction():
            documents.write(
                "retained-version.json", {"namespace": namespace, "value": 0.12345678912345678}
            )
            documents.write("current.json", "retained-version.json")
    from open_energy_planner.forecast_selection.storage import SelectionStore
    from open_energy_planner.solar.calculation.model import version

    model_store = SelectionStore(SqlDocuments(source, "planning"))
    capture = {
        "original_plan_id": "a",
        "start": "2026-10-20T00:00:00+00:00",
        "end": "2026-10-22T00:00:00+00:00",
    }
    evaluation = {
        "capture": version(capture),
        "start": capture["start"],
        "end": capture["end"],
        "baseline": {"economic_cost": 0.5, "corrected_import_kwh": 1},
        "candidate": {"economic_cost": 0.1, "corrected_import_kwh": 1},
    }
    pending_capture = {
        **capture,
        "original_plan_id": "B",
        "start": "2026-10-22T00:00:00+00:00",
        "end": "2026-10-24T00:00:00+00:00",
    }
    model_store.commit("family", {"windows": [], "pending": version(capture)}, capture=capture)
    model_store.commit(
        "family", {"windows": [version(evaluation)], "pending": None}, evaluation=evaluation
    )
    model_store.commit(
        "family",
        {"windows": [version(evaluation)], "pending": version(pending_capture)},
        capture=pending_capture,
    )
    obsolete = SqlPlanRepository(root / "target", engine=target)
    obsolete.state["plan"] = plan("target-history-to-overwrite")
    obsolete.save()
    with pytest.raises(ValueError, match="permission"):
        migrate_archive(source, target)
    assert obsolete.get_plan("target-history-to-overwrite") is not None
    counts = migrate_archive(source, target, overwrite_target=True)
    assert (
        counts["oep_documents"] == 22
    )  # Nine namespace pairs plus two model captures, one evaluation and their index.
    for namespace in DOCUMENT_NAMESPACES:
        documents = SqlDocuments(target, namespace)
        assert documents.read("current.json") == "retained-version.json"
        assert documents.read("retained-version.json") == {
            "namespace": namespace,
            "value": 0.12345678912345678,
        }
    restored_models = SelectionStore(SqlDocuments(target, "planning"))
    assert restored_models.state == model_store.state
    assert restored_models.read("capture", version(capture)) == capture
    assert restored_models.read("capture", version(pending_capture)) == pending_capture
    assert restored_models.read("evaluation", version(evaluation)) == evaluation
    assert counts["oep_archive_plans"] == 2
    assert (
        counts["oep_price_pairs"] == 168
        and counts["oep_price_actuals"] == 168
        and counts["oep_price_decisions"] == 6
    )
    assert SqlAlchemySelectionStore(target).load("NL") == expected_selection
    restored = SqlPlanRepository(root / "target", engine=target)
    assert restored.state == state
    assert restored.get_plan("a") == first and restored.get_plan("B") == second
    assert restored.get_plan("target-history-to-overwrite") is None
    assert original.get_plan("a") == first and original.state == state


def test_migration_failure_leaves_source_available_and_target_unactivated(route):
    """A server/write failure after destructive target DDL rolls back the copy and never damages the source."""
    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    original.state["plan"] = plan()
    original.save()

    def fail(_connection, _cursor, statement, _parameters, _context, _many):
        """Interrupt the copy after rebuilding target tables, before it can commit active state."""
        if statement.lstrip().upper().startswith("INSERT INTO OEP_ARCHIVE_VALUES"):
            raise OSError("fixture target outage")

    event.listen(target, "before_cursor_execute", fail)
    try:
        with pytest.raises(OSError):
            migrate_archive(source, target, overwrite_target=True)
    finally:
        event.remove(target, "before_cursor_execute", fail)
    assert original.get_plan("plan-a") == plan()
    with target.connect() as connection:
        for table in SCHEMA.sorted_tables:
            assert connection.scalar(select(func.count()).select_from(table)) == 0, table.name


def test_migration_rejects_database_alias_before_dropping_source(tmp_path):
    """Different SQLite URLs for the same file trigger the physical-alias probe and retain source content."""
    original = SqlPlanRepository(tmp_path)
    original.state["plan"] = plan()
    original.save()
    alias = create_engine(original.engine.url.update_query_dict({"timeout": "5"}))
    try:
        with pytest.raises(ValueError, match="same database"):
            migrate_archive(original.engine, alias, overwrite_target=True)
        assert original.get_plan("plan-a") == plan()
    finally:
        alias.dispose()
        original.close()


@pytest.mark.parametrize("backend", [name for name, _ in BACKENDS])
def test_startup_migration_changes_backend_only_after_verified_copy(tmp_path, backend):
    """The real startup path retains pairing/configuration on SQLite-to-server and server-to-SQLite transitions."""
    root = tmp_path / "app"
    first = SqlPlanRepository(root)
    first.state["plan"] = plan()
    first.state["enabled"] = False
    first.save()
    expected = deepcopy(first.state)
    for namespace in DOCUMENT_NAMESPACES:
        SqlDocuments(first.engine, namespace).write("retained.json", {"retained": True})
    first.close()
    if backend == "sqlite":
        reopened = SqlPlanRepository(root, migrate_database=True)
        try:
            assert reopened.state == expected
            assert reopened.get_plan("plan-a") == plan()
        finally:
            reopened.close()
        return
    target_url = URLS[backend]
    target = create_engine(target_url)
    SCHEMA.drop_all(target)
    try:
        migrated = SqlPlanRepository(root, database_url=target_url, migrate_database=True)
        assert migrated.state == expected
        migrated.close()
        old_source, offline = root / "archive.sqlite3", root / "source-offline.sqlite3"
        assert old_source.resolve().is_relative_to(
            root.resolve()
        ) and offline.resolve().is_relative_to(root.resolve())
        old_source.rename(offline)
        target_only = SqlPlanRepository(root, database_url=target_url)
        try:
            assert target_only.get_plan("plan-a") == plan()
            for namespace in DOCUMENT_NAMESPACES:
                assert SqlDocuments(target_only.engine, namespace).read("retained.json") == {
                    "retained": True
                }
            assert not old_source.exists()
        finally:
            target_only.close()
        assert json.loads((root / "archive-backend.json").read_text())["url"] == target_url
        restored = SqlPlanRepository(root, migrate_database=True)
        try:
            assert restored.state == expected and restored.get_plan("plan-a") == plan()
        finally:
            restored.close()
        assert not (root / "installation-settings-example.json").exists()
    finally:
        SCHEMA.drop_all(target)
        target.dispose()


@pytest.mark.parametrize("tamper", ["changed", "extra"])
def test_migration_verification_rejects_altered_or_extra_rows(route, tamper):
    """Exact content and row-count checks reject a corrupted copy on every route, retaining the readable source."""
    from open_energy_planner.storage.adapters.lifecycle.schema import VALUES

    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    original.state["plan"] = plan()
    original.save()

    def corrupt(connection, _cursor, statement, _parameters, _context, _many):
        """Alter the destination after copying the state, before independent row verification."""
        if statement.lstrip().upper().startswith("INSERT INTO OEP_ARCHIVE_STATE"):
            if tamper == "changed":
                connection.execute(VALUES.update().values(payload='"changed"'))
            else:
                connection.execute(VALUES.insert().values(digest="f" * 64, payload="null"))

    event.listen(target, "after_cursor_execute", corrupt)
    try:
        with pytest.raises(ValueError, match="verification failed"):
            migrate_archive(source, target, overwrite_target=True)
    finally:
        event.remove(target, "after_cursor_execute", corrupt)
    assert original.get_plan("plan-a") == plan()
    with target.connect() as connection:
        for table in SCHEMA.sorted_tables:
            assert connection.scalar(select(func.count()).select_from(table)) == 0, table.name


def test_migration_preserves_unrelated_generic_table_names(route):
    """OEP migration replaces only its own prefixed tables. An unrelated target pairs table survives every route unchanged."""
    from sqlalchemy import Column, MetaData, String, Table

    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    original.state["plan"] = plan()
    original.save()
    unrelated = Table("pairs", MetaData(), Column("owner", String(40), primary_key=True))
    unrelated.create(target)
    try:
        with target.begin() as connection:
            connection.execute(unrelated.insert().values(owner="unrelated-application"))
        migrate_archive(source, target, overwrite_target=True)
        with target.connect() as connection:
            assert connection.scalars(select(unrelated.c.owner)).all() == ["unrelated-application"]
    finally:
        unrelated.drop(target)


def test_original_forecast_quality_documents_transfer_without_source_dependency(route):
    """Daily pointers, immutable receipts and component bindings remain readable on every destination after the source is removed."""
    from datetime import UTC, datetime

    from open_energy_planner.forecast_quality.daily import (
        DailyForecastArchive,
        controlled_consumers,
    )
    from open_energy_planner.forecast_quality.history import HistoryArchive
    from tests.app.forecast.support.quality import START
    from tests.database.contracts.test_forecast_quality import SOURCE, receipt

    (source, target), root = route
    plans = SqlPlanRepository(root / "source", engine=source)
    original = HistoryArchive(SqlDocuments(source, "demand"))
    receipt(original, [(-1, 10), (0, 10.5), (1, 11.5)])
    original.documents.write(
        "quality-components-original.json", {"version": "same-issue", "selected": False}
    )
    daily = DailyForecastArchive(original.documents)
    midnight = START.replace(hour=0)
    context = {
        "forecast_quality_sources": [SOURCE],
        "input_snapshot": {"demand_evidence": [], "solar_evidence": []},
    }
    daily.observe(midnight - timedelta(seconds=3), "UTC", context)
    daily.observe(midnight + timedelta(seconds=2), "UTC", context)
    document = plan()
    document["forecast_quality_controlled"] = ["sensor.house"]
    plans.state["plan"] = document
    plans.save()
    published = datetime.now(UTC)
    expected_day = daily.read("2026-10-26", "UTC")
    expected = original.read(SOURCE, START, START + timedelta(hours=2))
    migrate_archive(source, target, overwrite_target=True)
    SCHEMA.drop_all(source)
    restored = HistoryArchive(SqlDocuments(target, "demand"))
    assert restored.read(SOURCE, START, START + timedelta(hours=2)) == expected
    assert expected[0] == {START: 0.5, START + timedelta(hours=1): 1}
    assert restored.documents.read("quality-components-original.json") == {
        "version": "same-issue",
        "selected": False,
    }
    migrated_day = DailyForecastArchive(restored.documents)
    assert migrated_day.read("2026-10-26", "UTC") == expected_day
    migrated_plans = SqlPlanRepository(root / "target", engine=target)
    assert controlled_consumers(
        migrated_plans, published - timedelta(days=1), published + timedelta(days=1)
    ) == ["sensor.house"]
    assert migrated_day.page(START, "UTC")["days"] == ["2026-10-26"]


def test_bonus_opening_origins_survive_every_backend_transfer(route):
    """Automatic and manual origins transfer with exact seeds and survive price-only book changes on all four engines."""
    from dataclasses import replace

    from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
    from open_energy_planner.contracts.bonus_history import BonusHistory
    from open_energy_planner.storage.counters import CounterStore
    from tests.app.history.support.bonus import NOW, START, history, inputs

    (source, target), root = route
    SqlPlanRepository(root / "source", engine=source)
    store = CounterStore(SqlDocuments(source, "bonus-counters"))
    contract, configuration = inputs()
    service = BonusHistory()
    request = service.request(contract, configuration, None, NOW)
    automatic = service.derive(request, history(), None, NOW)
    store.save_history(automatic, request=request, history=history())
    manual = CounterSeed("other", START, 12.5, 5, "sensor.export", NOW)
    book = CounterBook(NOW, (*automatic.seeds, manual))
    store.save_confirmation(book, seed=manual)
    automatic_origin = store.provenance(automatic.seeds[0])
    manual_origin = store.provenance(manual)
    assert automatic_origin["start_sum_kwh"] == 100
    assert automatic_origin["end_sum_kwh"] == 108
    assert manual_origin["source"] == "manual_confirmation"
    store.save(replace(book, recorded_at=NOW + timedelta(minutes=1)))
    assert store.provenance(automatic.seeds[0]) == automatic_origin
    migrate_archive(source, target, overwrite_target=True)
    restored = CounterStore(SqlDocuments(target, "bonus-counters"))
    assert restored.current.seeds == book.seeds
    assert restored.load(automatic.version) == automatic
    assert restored.provenance(automatic.seeds[0]) == automatic_origin
    assert restored.provenance(manual) == manual_origin
    assert store.current.seeds == book.seeds


def test_identified_price_comparisons_and_manual_preferences_transfer_without_source(route):
    """Every backend route preserves three-source evidence and manual settings after the source is removed."""
    from tests.database.contracts.test_jeroen_prices import NOW as PRICE_NOW
    from tests.database.contracts.test_jeroen_prices import make_selection, seed_quality

    (source, target), root = route
    SqlPlanRepository(root / "source", engine=source)
    original = make_selection(source, "NL")
    seed_quality(original)
    original.states = original.update((), PRICE_NOW)
    original.save_preferences([None, "third", "second"], None)
    expected = [original.state(bucket) for bucket in range(3)]
    counts = migrate_archive(source, target, overwrite_target=True)
    assert counts["oep_price_comparison_pairs"] == 336
    assert counts["oep_price_comparison_decisions"] >= 6
    SCHEMA.drop_all(source)
    restored = make_selection(target, "NL")
    assert restored.manual == (None, "third", "second")
    assert restored.configuration_version == original.configuration_version
    assert [restored.state(bucket) for bucket in range(3)] == expected


def test_monthly_bonus_survives_every_backend_transfer(route):
    """An October night range and disabled months survive target restart after disposing the source engine."""
    from open_energy_planner.contracts.storage import ContractStore
    from open_energy_planner.contracts.wire import parse_contract
    from tests.app.contract.test_bonus_windows import monthly_body

    engines, root = route
    source, target = engines
    repository = SqlPlanRepository(root / "source", engine=source)
    repository.save()
    original = parse_contract(monthly_body(), NOW)
    store = ContractStore(SqlDocuments(source, "contracts"))
    store.save(original)
    migrate_archive(source, target, overwrite_target=True)
    source.dispose()
    target_url = target.url
    target.dispose()
    engines[1] = create_engine(target_url)
    restored = ContractStore(SqlDocuments(engines[1], "contracts"))
    assert restored.issue is None
    assert restored.current == original
    assert restored.current.version == original.version
    assert restored.load(original.version) == original


@pytest.mark.parametrize("known_prefix", [False, True])
def test_statistics_prefix_and_correction_provenance_survive_backend_transfer(route, known_prefix):
    """Every backend route retains references and rejected corrections without depending on the source."""
    from dataclasses import replace

    from open_energy_planner.contracts.bonus_history import BonusHistory
    from open_energy_planner.storage.counters import CounterStore
    from tests.app.history.support.bonus import NOW as COUNTER_NOW
    from tests.app.history.support.bonus import inputs
    from tests.app.history.test_bonus_statistics import derive, report

    (source, target), root = route
    SqlPlanRepository(root / "source", engine=source).save()
    store = CounterStore(SqlDocuments(source, "bonus-counters"))
    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, COUNTER_NOW)
    original = derive() if known_prefix else derive(report(request, offset=2, state=False))
    seed = original.seeds[0]
    basis = (
        replace(seed.statistics, prefix_kwh=3500, confirmed_at=COUNTER_NOW)
        if known_prefix
        else seed.statistics
    )
    confirmed = (
        replace(original, seeds=(replace(seed, statistics=basis, used_kwh=3508),))
        if known_prefix
        else original
    )
    store.save_statistics(confirmed)
    invalid = replace(
        confirmed,
        seeds=(
            replace(
                confirmed.seeds[0],
                statistics=replace(
                    basis,
                    issue="bonus_statistics_revalidation_required",
                    rejected_digest="b" * 64,
                ),
            ),
        ),
    )
    store.save_statistics(invalid)
    migrate_archive(source, target, overwrite_target=True)
    SCHEMA.drop_all(source)
    restored = CounterStore(SqlDocuments(target, "bonus-counters"))
    assert restored.current == invalid
    initial = restored.load(confirmed.version).seeds[0]
    if known_prefix:
        assert initial.statistics.used(COUNTER_NOW) == 3508
    else:
        assert initial.used_kwh is None
        assert initial.meter_kwh is None
        assert initial.statistics.prefix_kwh is None
    origin = restored.provenance(invalid.seeds[0])
    assert origin["supersedes"] == confirmed.version
    assert origin["statistics"]["rejected_digest"] == "b" * 64


def test_calculation_day_evidence_transfers_without_source_dependency(route):
    """Each backend route retains failed calculation evidence with unknown timing and no progress."""
    from datetime import UTC, datetime

    from open_energy_planner.optimization.performance import CalculationHistory
    from open_energy_planner.synthetic import capture

    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    SqlPlanRepository(root / "target", engine=target)
    started = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    history = CalculationHistory(SqlDocuments(source, "planning"))
    history.record(
        capture(started), None, started, started + timedelta(seconds=2), "planning_error"
    )
    expected = history.day("2026-10-25", "Europe/Amsterdam")
    original_detail = history.detail(expected[0]["id"])
    migrate_archive(source, target, overwrite_target=True)
    original.close()
    source.dispose()
    actual = CalculationHistory(SqlDocuments(target, "planning")).day(
        "2026-10-25", "Europe/Amsterdam"
    )
    assert actual == expected
    restored = CalculationHistory(SqlDocuments(target, "planning"))
    assert restored.detail(actual[0]["id"]) == original_detail
    assert "progress" not in actual[0]
    assert restored.detail(actual[0]["id"])["progress"] == []
    assert actual[0]["first_candidate_seconds"] is None


def test_finite_calculation_progress_transfers_without_source_dependency(route):
    """Every backend route retains exact independently specified zero, negative cost and finite bound samples."""
    from datetime import UTC, datetime

    from open_energy_planner.optimization.domain import SolveResult, SolveSample, Status
    from open_energy_planner.optimization.performance import CalculationHistory
    from open_energy_planner.planning import PlanningOutcome
    from open_energy_planner.synthetic import capture

    (source, target), root = route
    original = SqlPlanRepository(root / "source", engine=source)
    SqlPlanRepository(root / "target", engine=target)
    started = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    result = SolveResult(
        Status.FEASIBLE,
        None,
        "time_limit",
        1,
        3,
        -0.6,
        progress=(SolveSample(2, 0, None), SolveSample(4, -0.5, -0.6)),
        first_candidate_seconds=2,
        time_limit_seconds=120,
    )
    history = CalculationHistory(SqlDocuments(source, "planning"))
    history.record(
        capture(started),
        PlanningOutcome(None, "candidate_rejected", result),
        started,
        started + timedelta(seconds=4),
        "candidate_rejected",
    )
    identity = history.day("2026-10-25", "Europe/Amsterdam")[0]["id"]
    migrate_archive(source, target, overwrite_target=True)
    original.close()
    source.dispose()
    restored = CalculationHistory(SqlDocuments(target, "planning"))
    assert "progress" not in restored.day("2026-10-25", "Europe/Amsterdam")[0]
    assert restored.detail(identity)["progress"] == [
        {"elapsed_seconds": 2, "objective": 0, "best_bound": None},
        {"elapsed_seconds": 4, "objective": -0.5, "best_bound": -0.6},
    ]
    assert restored.detail(identity)["first_candidate_seconds"] == 2


def test_nonlinear_solar_archive_migrates_and_replays_without_source_database(route):
    """Every backend route preserves native trees and raw validation/exclusion evidence for source-independent replay."""
    from open_energy_planner.solar.backtest import HistoricalForecastAssessment
    from open_energy_planner.solar.boosting import SolarBoostedModel
    from open_energy_planner.solar.calculation.model import version
    from open_energy_planner.solar.storage import SolarStore
    from open_energy_planner.solar.storage.forecast_weather import ForecastWeatherCache
    from open_energy_planner.solar.trend import YieldTrend
    from open_energy_planner.solar.weather import forecast_weather_query
    from open_energy_planner.solar.wire import document
    from tests.app.solar.test_boosting import fit_case
    from tests.database.support.solar import forecast_evidence

    (source, target), root = route
    SqlPlanRepository(root / "source", engine=source)
    from tests.app.solar.support import SITE

    result, history, _physical, weather, now = fit_case(corrupt=True)
    result = replace(
        result, model=replace(result.model, time_trend=YieldTrend(-0.005, now - timedelta(days=30)))
    )
    store = SolarStore(SqlDocuments(source, "solar"))
    model = document(result.model)
    weights = {"schema": model["schema"], "model_json": model.pop("model_json")}
    weights_reference = version(weights)
    model["weights_reference"] = weights_reference
    store.archive("ml-weights", weights_reference, weights)
    report = {"model": model, "summary": document(result.summary)}
    store.archive("ml-training", result.model.version, report)
    store.archive("history", "original", document(history))
    forecast_site, original_run, assessment = forecast_evidence()
    forecast_query = forecast_weather_query(forecast_site, original_run.initialized_at)
    ForecastWeatherCache(store).write(forecast_query, original_run)
    store.archive("historical-assessment", "synthetic", document(assessment))
    migrate_archive(source, target, overwrite_target=True)
    source.dispose()
    restored = SolarStore(SqlDocuments(target, "solar"))
    assert ForecastWeatherCache(restored).read(forecast_query) == original_run
    assert (
        HistoricalForecastAssessment.restore(
            restored.documents.read("historical-assessment-synthetic.json")
        )
        == assessment
    )
    copied = restored.documents.read("ml-training-" + result.model.version + ".json")
    assert copied == report
    assert restored.documents.read("history-original.json") == document(history)
    copied_weights = restored.documents.read("ml-weights-" + weights_reference + ".json")
    assert copied_weights == weights
    replay = SolarBoostedModel.restore(
        {
            **{key: value for key, value in copied["model"].items() if key != "weights_reference"},
            "model_json": copied_weights["model_json"],
        }
    )
    assert replay.predict(weather, SITE, known_at=now) == result.model.predict(
        weather, SITE, known_at=now
    )
    assert copied["summary"]["excluded_counts"]["production_out_of_range"] == 1
    assert copied["summary"]["excluded_counts"].get("statistical_outlier", 0) == 0
    assert copied["summary"]["diagnostic_counts"]["irradiance_yield_anomaly"] >= 1
