"""Prove daily source-bound training, immutable reports and restart without redundant fitting."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.solar.boosting import TrainingResult, TrainingStatus, TrainingSummary
from open_energy_planner.solar.calculation.model import version
from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.training import DailySolarTraining
from open_energy_planner.solar.wire import document
from tests.app.solar.boosting_support import runtime_case
from tests.app.solar.support import SITE, plant, weather
from tests.database.support.archive import list_documents, make_store

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def training_case(tmp_path, status=TrainingStatus.BASELINE_PREFERRED):
    """Persist one synthetic completed attempt with historical inputs entirely before both daily cutoffs."""
    store = make_store(tmp_path, SolarStore)
    definition = plant()
    store.save(definition)
    learner = DailySolarTraining(store)
    history = EnergyHistory(
        "sensor.pv0",
        tuple(EnergyHour(NOW - timedelta(hours=120 - index), index, index) for index in range(48)),
    )
    arguments = (definition.sources[0], definition.inverters[0], SITE, history, (), (), None)
    cached, token = learner.prepare(*arguments, NOW)
    assert cached is None
    result = TrainingResult(None, TrainingSummary(status, 47, 40, 7, {}, ()))
    original = learner.save(token, result, {"model": None, "summary": document(result.summary)})
    return store, learner, arguments, original


def test_successful_daily_attempt_does_not_create_reports_per_forecast(tmp_path):
    """Two hundred same-day refreshes preserve the original creation evidence and a single report."""
    store, learner, arguments, original = training_case(tmp_path)
    before = list_documents(tmp_path, "solar")
    for minute in range(200):
        cached, _token = learner.prepare(*arguments, NOW + timedelta(minutes=minute))
        assert cached == original
    assert list_documents(tmp_path, "solar") == before
    assert len([name for name in before if name.startswith("ml-training-")]) == 1
    assert store.documents.read("ml-training-" + original.archive_reference + ".json")[
        "summary"
    ] == document(original.summary)


def test_unchanged_completed_data_reuses_report_on_following_day(tmp_path):
    """A calendar transition without new admitted history does not require another native fit."""
    store, _learner, arguments, original = training_case(tmp_path)
    restored = DailySolarTraining(SolarStore(store.documents))
    cached, _token = restored.prepare(*arguments, NOW + timedelta(days=1))
    assert cached == original
    assert (
        len([name for name in list_documents(tmp_path, "solar") if name.startswith("ml-training-")])
        == 1
    )


def test_following_day_with_new_completed_data_requests_training(tmp_path):
    """New completed source hours become a new daily cohort without rewriting the old report."""
    _store, learner, arguments, _original = training_case(tmp_path)
    history = arguments[3]
    changed = replace(history, hours=(*history.hours, EnergyHour(NOW, 49, 49)))
    cached, token = learner.prepare(
        *arguments[:3], changed, *arguments[4:], NOW + timedelta(days=1)
    )
    assert cached is None
    assert token["previous"] is not None


def test_source_definition_change_cannot_reuse_another_binding(tmp_path):
    """A changed capacity invalidates same-day model reuse immediately and has no unsafe fallback."""
    _store, learner, arguments, _original = training_case(tmp_path)
    source = replace(arguments[0], capacity_w=2000)
    cached, token = learner.prepare(source, *arguments[1:], NOW + timedelta(minutes=15))
    assert cached is None
    assert token["previous"] is None
    assert learner.fallback(token) is None


@pytest.mark.parametrize(
    "status", [TrainingStatus.INSUFFICIENT_HISTORY, TrainingStatus.BLOCKED, TrainingStatus.FAILED]
)
def test_incomplete_attempt_retries_only_when_admitted_inputs_change(tmp_path, status):
    """Unchanged missing data does not flood reports, while a completed historical import permits a retry."""
    _store, learner, arguments, original = training_case(tmp_path, status)
    assert learner.prepare(*arguments, NOW + timedelta(minutes=15))[0] == original
    history = arguments[3]
    changed = replace(history, hours=(*history.hours, EnergyHour(NOW - timedelta(days=1), 49, 49)))
    assert (
        learner.prepare(*arguments[:3], changed, *arguments[4:], NOW + timedelta(minutes=30))[0]
        is None
    )


def test_corrupt_report_does_not_become_a_valid_cache_hit(tmp_path):
    """Restart refuses modified historical evidence rather than silently treating it as an original fit."""
    store, _learner, arguments, original = training_case(tmp_path)
    name = "ml-training-" + original.archive_reference + ".json"
    report = store.documents.read(name)
    report["summary"]["training_hours"] = 999
    store.documents.write(name, report)
    with pytest.raises(ValueError, match="checksum"):
        DailySolarTraining(SolarStore(store.documents)).prepare(*arguments, NOW)


@pytest.mark.parametrize(
    "status", [TrainingStatus.INSUFFICIENT_VALIDATION, TrainingStatus.BASELINE_PREFERRED]
)
def test_utc_validation_rollover_retries_only_incomplete_local_day_attempt(tmp_path, status):
    """Amsterdam's same local date crosses UTC midnight, permitting an incomplete validation retry."""
    _store, learner, arguments, original = training_case(tmp_path, status)
    local_midnight = datetime(2026, 8, 20, 22, 5, tzinfo=UTC)
    assert learner.prepare(*arguments, local_midnight)[0] == original
    cached, token = learner.prepare(*arguments, local_midnight + timedelta(hours=2))
    if status == TrainingStatus.INSUFFICIENT_VALIDATION:
        assert cached is None
        assert token["validation_end"] == "2026-08-21T00:00:00+00:00"
    else:
        assert cached == original


@pytest.mark.parametrize(
    "status", [TrainingStatus.INSUFFICIENT_VALIDATION, TrainingStatus.BASELINE_PREFERRED]
)
def test_forecast_reuse_keeps_daily_validation_retry(tmp_path, monkeypatch, status):
    """Keep reusing an issued forecast across UTC midnight after a completed local-day attempt."""
    store = make_store(tmp_path, SolarStore)
    store.save(plant())
    runtime = SolarRuntime(store)
    before = datetime(2026, 8, 20, 23, 45, tzinfo=UTC)
    after = before + timedelta(minutes=15)
    runtime.set_weather(SITE, weather(before, days=5))
    runtime.histories["sensor.pv0"] = EnergyHistory(
        "sensor.pv0",
        (EnergyHour(NOW - timedelta(hours=48), 0, 0), EnergyHour(NOW - timedelta(hours=47), 1, 1)),
    )
    attempts = []

    def fit(_source, _inverter, _location, _history, _hourly, _weather, now, **_options):
        """Record only fits admitted by the real runtime and training store."""
        attempts.append(now)
        return TrainingResult(None, TrainingSummary(status, 1, 0, 0, {}, ()))

    monkeypatch.setattr("open_energy_planner.solar.runtime.fit_boosted_model", fit)
    assert runtime.install(runtime.build(runtime.job(before)), before)
    forecast_version = runtime.forecast.version
    job = runtime.job(after)
    if status == TrainingStatus.INSUFFICIENT_VALIDATION:
        assert job is not None
        assert runtime.install(runtime.build(job), after)
        assert attempts == [before, after]
    else:
        assert job is None
        assert runtime.forecast.version == forecast_version
        assert attempts == [before]


def test_native_model_survives_runtime_restart_and_new_weather(tmp_path, monkeypatch):
    """A restored runtime predicts a fresh issue using checked stored weights without another fit."""
    runtime, now = runtime_case(tmp_path)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    original = runtime.learning["pv0"]
    assert original.model is not None
    references = list_documents(tmp_path, "solar")

    def unexpected_training(*arguments, **keywords):
        """Fail if current weather or an ordinary restart triggers native training again."""
        raise AssertionError("A same-day model must be restored")

    monkeypatch.setattr("open_energy_planner.solar.runtime.fit_boosted_model", unexpected_training)
    restored = SolarRuntime(SolarStore(runtime.store.documents))
    assert restored.forecast is None
    restored.histories = runtime.histories
    restored.set_weather(
        SITE, tuple(replace(row, celsius=row.celsius + 1) for row in runtime.weather)
    )
    restored.set_forecast_runs(SITE, runtime.forecast_runs)
    later = now + timedelta(minutes=15)
    assert restored.install(restored.build(restored.job(later)), later)
    assert restored.learning["pv0"] == original
    assert restored.forecast.issued_at == later
    assert len(
        [name for name in list_documents(tmp_path, "solar") if name.startswith("ml-training-")]
    ) == len([name for name in references if name.startswith("ml-training-")])


def test_inadequate_new_attempt_preserves_usable_model_after_restart(tmp_path):
    """A failed or insufficient new cohort cannot replace a previously usable same-source model."""
    runtime, now = runtime_case(tmp_path)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    original = runtime.learning["pv0"]
    definition = runtime.store.current
    source = definition.sources[0]
    history = runtime.histories[source.entity_id]
    arguments = (
        source,
        definition.inverters[0],
        SITE,
        history,
        runtime.weather,
        runtime.forecast_runs,
        None,
    )
    learner = DailySolarTraining(runtime.store)
    cached, token = learner.prepare(*arguments, now + timedelta(days=1))
    assert cached is None
    assert learner.fallback(token) == original
    insufficient = TrainingResult(
        None, TrainingSummary(TrainingStatus.INSUFFICIENT_HISTORY, 1, 0, 0, {}, ())
    )
    fallback = learner.save(
        token, insufficient, {"model": None, "summary": document(insufficient.summary)}
    )
    assert fallback == original
    assert fallback.attempt_status == TrainingStatus.INSUFFICIENT_HISTORY
    restored = DailySolarTraining(SolarStore(runtime.store.documents))
    cached, _token = restored.prepare(*arguments, now + timedelta(days=1, minutes=15))
    assert cached == original
    assert cached.attempt_status == TrainingStatus.INSUFFICIENT_HISTORY


@pytest.mark.parametrize(
    "field,value",
    [
        ("report", []),
        ("report", "bad"),
        ("inputs", {}),
        ("inputs", "bad"),
        ("usable", []),
        ("usable", "bad"),
        ("day", "2026-08-19garbage"),
        ("validation_end", []),
        ("validation_end", "2026-08-20T00:00:00"),
        ("validation_end", "2026-08-20T01:00:00+00:00"),
        ("validation_end", "2026-08-21T00:00:00+00:00"),
    ],
)
def test_malformed_pointer_fields_are_explicit_value_errors(tmp_path, field, value):
    """Invalid persisted hashes, calendar values and validation bounds cannot escape forecast recovery."""
    store, _learner, arguments, _original = training_case(tmp_path)
    [name] = [
        name for name in list_documents(tmp_path, "solar") if name.startswith("training-current-")
    ]
    pointer = store.documents.read(name)
    store.documents.write(name, {**pointer, field: value})
    with pytest.raises(ValueError, match="Invalid solar training pointer"):
        DailySolarTraining(SolarStore(store.documents)).prepare(*arguments, NOW)


def test_invalid_pointer_keeps_physical_runtime_forecast_available(tmp_path):
    """A malformed usable reference is handled as failed ML while the independently computed forecast remains usable."""
    runtime, now = runtime_case(tmp_path)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    [name] = [
        name for name in list_documents(tmp_path, "solar") if name.startswith("training-current-")
    ]
    pointer = runtime.store.documents.read(name)
    runtime.store.documents.write(name, {**pointer, "usable": []})
    later = now + timedelta(minutes=15)
    runtime.set_weather(SITE, tuple(replace(row, ghi=0) for row in runtime.weather))
    assert runtime.install(runtime.build(runtime.job(later)), later)
    assert runtime.forecast is not None
    assert runtime.learning["pv0"].model is None
    assert runtime.learning["pv0"].summary.status == TrainingStatus.FAILED


@pytest.mark.parametrize("failure", ["revision_write", "snapshot_read"])
def test_withdrawn_source_hours_revoke_persisted_model_and_fallback_after_restart(
    tmp_path, monkeypatch, failure
):
    """An accepted-history withdrawal atomically changes the binding and survives runtime restoration."""
    from open_energy_planner.solar.storage.history import parse_report
    from tests.app.solar.history_support import HISTORY_IDENTITY, discover
    from tests.app.solar.support import weather

    runtime, now = runtime_case(tmp_path)
    history = runtime.histories["sensor.pv0"]
    runtime.histories = {}
    imported_at = now
    discover(runtime, imported_at, first=history.hours[0].start.isoformat())
    pending = runtime.request(imported_at)
    end = datetime.fromisoformat(pending["end"])
    rows = [
        [hour.start.timestamp(), hour.total_kwh, hour.total_kwh]
        for hour in history.hours
        if hour.start < end
    ]

    def transfer(request, values):
        """Submit original raw endpoints through the real source-bound importer envelope."""
        return parse_report(
            {
                "basis": "solar_history_chunk",
                "request_version": request["version"],
                "entity_id": "sensor.pv0",
                "statistics_identity": HISTORY_IDENTITY,
                "unit": "kWh",
                "hours": values,
            }
        )

    report = transfer(pending, rows)
    runtime.receive(pending["version"], report.history, imported_at, report=report)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    original = runtime.learning["pv0"]
    assert original.model is not None
    before = runtime.training.revision("sensor.pv0")
    withdrawn_at = now + timedelta(days=1)
    pending = runtime.request(withdrawn_at)
    revised = transfer(pending, rows[:100] + rows[102:])
    accepted_before = runtime.histories["sensor.pv0"]
    original_write = runtime.store.documents.write

    def interrupt_revision(name, value):
        """Fail the generation update after accepted-history writes to prove their shared rollback."""
        if name.startswith("training-evidence-"):
            raise OSError("Synthetic evidence revision write failure")
        return original_write(name, value)

    def interrupt_snapshot(entities):
        """Fail snapshot restoration after the proposed history and evidence generation have both changed."""
        raise OSError("Synthetic evidence revision snapshot read failure")

    with monkeypatch.context() as patch:
        if failure == "revision_write":
            patch.setattr(runtime.store.documents, "write", interrupt_revision)
        else:
            patch.setattr(runtime.training, "revisions", interrupt_snapshot)
        with pytest.raises(OSError, match="Synthetic evidence revision"):
            runtime.receive(pending["version"], revised.history, withdrawn_at, report=revised)
    assert runtime.training.revision("sensor.pv0") == before
    assert runtime.histories["sensor.pv0"] == accepted_before
    assert (
        SolarRuntime(SolarStore(runtime.store.documents)).histories["sensor.pv0"] == accepted_before
    )
    runtime.receive(pending["version"], revised.history, withdrawn_at, report=revised)
    runtime.set_weather(
        SITE,
        tuple(
            replace(row, received_at=withdrawn_at)
            for row in (
                *tuple(row for row in runtime.weather if row.start < now),
                *weather(now, days=5),
            )
        ),
    )
    assert runtime.training.revision("sensor.pv0") == before + 1

    def insufficient(*args, **kwargs):
        """Make fallback selection observable rather than relying on a newly admitted native model."""
        return TrainingResult(
            None, TrainingSummary(TrainingStatus.INSUFFICIENT_HISTORY, 1, 0, 0, {}, ())
        )

    monkeypatch.setattr("open_energy_planner.solar.runtime.fit_boosted_model", insufficient)
    later = withdrawn_at + timedelta(minutes=15)
    assert runtime.install(runtime.build(runtime.job(later)), later)
    assert runtime.learning["pv0"].model is None
    assert runtime.learning["pv0"].archive_reference != original.archive_reference
    restored = SolarRuntime(SolarStore(runtime.store.documents))
    restored.set_weather(SITE, runtime.weather)
    restored.set_forecast_runs(SITE, runtime.forecast_runs)
    later += timedelta(minutes=15)
    assert restored.install(restored.build(restored.job(later)), later)
    assert restored.learning["pv0"].model is None
    assert restored.training.revision("sensor.pv0") == before + 1


def test_original_run_withdrawal_is_durable_and_does_not_restore_last_usable_model(
    tmp_path, monkeypatch
):
    """Replacing a restarted runtime's original-run cohort revokes same-day cached weights and fallback."""
    runtime, now = runtime_case(tmp_path)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.learning["pv0"].model is not None
    before = runtime.run_evidence_revision
    restored = SolarRuntime(SolarStore(runtime.store.documents))
    restored.histories = runtime.histories
    restored.set_weather(SITE, runtime.weather)
    restored.set_forecast_runs(SITE, runtime.forecast_runs[1:])
    assert restored.run_evidence_revision == before + 1

    def insufficient(*args, **kwargs):
        """A withdrawn training cohort cannot fall back to its former usable model."""
        return TrainingResult(
            None, TrainingSummary(TrainingStatus.INSUFFICIENT_HISTORY, 1, 0, 0, {}, ())
        )

    monkeypatch.setattr("open_energy_planner.solar.runtime.fit_boosted_model", insufficient)
    later = now + timedelta(minutes=15)
    assert restored.install(restored.build(restored.job(later)), later)
    assert restored.learning["pv0"].model is None


def test_worker_captured_before_withdrawal_cannot_publish_under_new_revision(tmp_path, monkeypatch):
    """An obsolete worker retains its captured evidence generation even if destructive imports happen before it runs."""
    runtime, now = runtime_case(tmp_path)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    original = runtime.learning["pv0"]
    later = now + timedelta(minutes=15)
    runtime.set_weather(SITE, tuple(replace(row, ghi=0) for row in runtime.weather))
    stale_job = runtime.job(later)
    assert stale_job is not None
    runtime.set_forecast_runs(SITE, runtime.forecast_runs[1:])

    def insufficient(*args, **kwargs):
        """Expose accidental stale-model fallback under the replacement generation."""
        return TrainingResult(
            None, TrainingSummary(TrainingStatus.INSUFFICIENT_HISTORY, 1, 0, 0, {}, ())
        )

    monkeypatch.setattr("open_energy_planner.solar.runtime.fit_boosted_model", insufficient)
    stale_result = runtime.build(stale_job)
    assert stale_result[-1]["pv0"].archive_reference == original.archive_reference
    assert not runtime.install(stale_result, later)
    later += timedelta(minutes=15)
    assert runtime.install(runtime.build(runtime.job(later)), later)
    assert runtime.learning["pv0"].model is None


@pytest.mark.parametrize("historical", [False, True])
def test_feedback_only_revises_training_for_changed_historical_observations(
    tmp_path, historical, monkeypatch
):
    """Appending current curtailment transitions preserves daily reuse. Changed prior-day classifications revoke it."""
    from open_energy_planner.solar.curtailment import CurtailmentHistory, save_feedback

    entity = "binary_sensor.pv_curtailed"
    definition = plant()
    definition = replace(
        definition, sources=(replace(definition.sources[0], curtailment_entity_id=entity),)
    )
    store = make_store(tmp_path, SolarStore)
    store.save(definition)
    start = NOW - timedelta(days=1) if historical else NOW
    save_feedback(store, None, CurtailmentHistory(entity, start, ()), start - timedelta(hours=1))
    runtime = SolarRuntime(store)
    until = start + timedelta(minutes=5)
    body = {
        "configuration_version": definition.version,
        "start": start.isoformat(),
        "feedback": {
            "entity_id": entity,
            "checked_until": until.isoformat(),
            "readings": [[start.isoformat(), True]],
        },
    }

    def unreadable_snapshot(entities):
        """Reject the proposed feedback and epoch atomically when their memory snapshot cannot be restored."""
        raise OSError("Synthetic feedback snapshot read failure")

    with monkeypatch.context() as patch:
        patch.setattr(runtime.training, "revisions", unreadable_snapshot)
        with pytest.raises(OSError, match="feedback snapshot"):
            runtime.receive_feedback(body, NOW + timedelta(minutes=5))
    assert runtime.feedback[entity].checked_until == start
    assert runtime.evidence_revisions == {"sensor.pv0": 0}
    reopened = SolarRuntime(SolarStore(store.documents))
    assert reopened.feedback[entity].checked_until == start
    assert reopened.training.revision("sensor.pv0") == 0
    runtime.receive_feedback(body, NOW + timedelta(minutes=5))
    assert runtime.training.revision("sensor.pv0") == int(historical)
    assert SolarRuntime(SolarStore(store.documents)).training.revision("sensor.pv0") == int(
        historical
    )


def test_restarted_open_meteo_reader_admits_partial_cohorts_without_retraining(
    tmp_path, monkeypatch
):
    """A live-only restart and four-run cache batches retain saved model generations without bulk restoration."""
    import asyncio

    from open_energy_planner.solar.storage.forecast_weather import ForecastWeatherCache
    from open_energy_planner.solar.weather import OpenMeteoSolarSource, forecast_weather_query
    from tests.app.solar.test_forecast_weather import LatestRunSession

    runtime, now = runtime_case(tmp_path)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    original = runtime.learning["pv0"]
    assert original.model is not None
    cache = ForecastWeatherCache(runtime.store)
    for run in runtime.forecast_runs:
        cache.write(forecast_weather_query(SITE, run.initialized_at), run)
    original_revision = runtime.training.revision("sensor.pv0")
    reports = {
        name for name in list_documents(tmp_path, "solar") if name.startswith("ml-training-")
    }
    initialized = now - timedelta(hours=6)
    session = LatestRunSession(
        metadata={
            "last_run_initialisation_time": initialized.timestamp(),
            "last_run_availability_time": (initialized + timedelta(hours=1)).timestamp(),
        }
    )
    reader = OpenMeteoSolarSource(
        session, runtime.store, history_start=lambda: now - timedelta(days=56)
    )
    assert reader.forecast_runs == ()
    restored = SolarRuntime(SolarStore(runtime.store.documents))
    restored.histories = runtime.histories
    loaded = []
    original_read = ForecastWeatherCache.read

    def observe_cache_read(cache, query, origin=None):
        """Count bounded individual run reads instead of allowing a startup cohort scan."""
        loaded.append(query["initialized_at"])
        return original_read(cache, query, origin)

    def unexpected_training(*args, **kwargs):
        """A partially loaded unchanged archive must restore the saved native model without fitting."""
        raise AssertionError("Partial Open-Meteo cache loading must preserve the daily model")

    monkeypatch.setattr(ForecastWeatherCache, "read", observe_cache_read)
    monkeypatch.setattr("open_energy_planner.solar.runtime.fit_boosted_model", unexpected_training)
    weather = asyncio.run(reader.read(SITE, now))
    assert len(reader.forecast_runs) == 1 and len(loaded) == 2
    assert len(set(loaded)) == 1
    restored.set_weather(SITE, weather, service=reader.service)
    restored.set_forecast_runs(SITE, reader.forecast_runs, authoritative=False)
    later = now + timedelta(minutes=15)
    assert restored.install(restored.build(restored.job(later)), later)
    assert restored.learning["pv0"].archive_reference == original.archive_reference
    for _batch in range(2):
        before = len(loaded)
        runs = asyncio.run(reader.import_runs(SITE, now))
        assert len(loaded) - before == 4
        restored.set_forecast_runs(SITE, runs, authoritative=False)
        later += timedelta(minutes=15)
        assert restored.install(restored.build(restored.job(later)), later)
        assert restored.learning["pv0"].archive_reference == original.archive_reference
        assert restored.training.revision("sensor.pv0") == original_revision
    assert {
        name for name in list_documents(tmp_path, "solar") if name.startswith("ml-training-")
    } == reports


def test_partial_run_admissions_preserve_unloaded_keys_but_replacement_and_withdrawal_revoke(
    tmp_path,
):
    """Absence from an admission batch is harmless. Changed supplied keys and authoritative omissions invalidate."""
    runtime, _now = runtime_case(tmp_path)
    original = runtime.forecast_runs
    before = runtime.run_evidence_revision
    selected = original[-1:]
    runtime.set_forecast_runs(SITE, selected, authoritative=False)
    assert runtime.forecast_runs == original
    assert runtime.run_evidence_revision == before
    changed = replace(
        selected[0],
        hours=(
            replace(selected[0].hours[0], ghi=selected[0].hours[0].ghi + 1),
            *selected[0].hours[1:],
        ),
    )
    runtime.set_forecast_runs(SITE, (changed,), authoritative=False)
    assert runtime.run_evidence_revision == before + 1
    assert len(runtime.forecast_runs) == len(original)
    runtime.set_forecast_runs(SITE, (), authoritative=True)
    assert runtime.forecast_runs == ()
    assert runtime.run_evidence_revision == before + 2


def test_run_replacement_while_source_is_absent_revokes_its_restored_daily_model(
    tmp_path, monkeypatch
):
    """A site's evidence epoch protects source A even when only source B is configured during replacement."""
    runtime, now = runtime_case(tmp_path)
    definition = runtime.configuration
    histories = dict(runtime.histories)
    histories["sensor.pv1"] = replace(histories["sensor.pv0"], entity_id="sensor.pv1")
    runtime.histories = dict(histories)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    original = runtime.learning["pv0"]
    assert original.model is not None and runtime.learning["pv1"].model is not None
    initial = runtime.run_evidence_revision
    runtime.configure(
        replace(definition, sources=(definition.sources[1],), inverters=(definition.inverters[1],))
    )
    runtime.histories = {"sensor.pv1": histories["sensor.pv1"]}
    run = runtime.forecast_runs[0]
    changed = replace(run, hours=(replace(run.hours[0], ghi=run.hours[0].ghi + 1), *run.hours[1:]))
    runtime.set_forecast_runs(SITE, (changed,), authoritative=False)
    assert runtime.run_evidence_revision == initial + 1
    assert runtime.training.revision("sensor.pv0") == 0
    runtime.configure(definition)
    runtime.histories = histories

    def insufficient(*args, **kwargs):
        """A newly invalidated source cannot recover the usable fallback from its former site generation."""
        return TrainingResult(
            None, TrainingSummary(TrainingStatus.INSUFFICIENT_HISTORY, 1, 0, 0, {}, ())
        )

    monkeypatch.setattr("open_energy_planner.solar.runtime.fit_boosted_model", insufficient)
    later = now + timedelta(minutes=15)
    assert runtime.install(runtime.build(runtime.job(later)), later)
    assert runtime.learning["pv0"].model is None
    assert runtime.learning["pv0"].archive_reference != original.archive_reference


def test_run_admission_snapshot_read_failure_rolls_back_and_unchanged_retry_repairs_memory(
    tmp_path, monkeypatch
):
    """Cohort and epochs commit only after the returned memory snapshot is readable, including unchanged retries."""
    runtime, _now = runtime_case(tmp_path)
    original_runs = runtime.forecast_runs
    initial = runtime.run_evidence_revision
    run = original_runs[0]
    changed = replace(run, hours=(replace(run.hours[0], ghi=run.hours[0].ghi + 1), *run.hours[1:]))

    def unreadable_snapshot(entities):
        """Interrupt after proposed cohort and site-generation writes, before their commit."""
        raise OSError("Synthetic admission snapshot read failure")

    with monkeypatch.context() as patch:
        patch.setattr(runtime.training, "revisions", unreadable_snapshot)
        with pytest.raises(OSError, match="admission snapshot"):
            runtime.set_forecast_runs(SITE, (changed,), authoritative=False)
    assert runtime.forecast_runs == original_runs
    assert runtime.run_evidence_revision == initial
    assert runtime.training.site_revision(version(document(SITE))) == initial
    runtime.set_forecast_runs(SITE, (changed,), authoritative=False)
    assert runtime.run_evidence_revision == initial + 1
    assert runtime.training.site_revision(version(document(SITE))) == initial + 1
    runtime.run_evidence_revision = initial
    runtime.evidence_revisions = {"sensor.pv0": 999}
    runtime.learning = {"pv0": object()}
    runtime.set_forecast_runs(SITE, runtime.forecast_runs, authoritative=False)
    assert runtime.run_evidence_revision == initial + 1
    assert runtime.evidence_revisions == {"sensor.pv0": 0, "sensor.pv1": 0}
    assert runtime.learning == {}
    assert runtime.training.site_revision(version(document(SITE))) == initial + 1
