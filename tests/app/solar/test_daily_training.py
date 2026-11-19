"""Prove daily source-bound training, immutable reports and restart without redundant fitting."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.solar.boosting import TrainingResult, TrainingStatus, TrainingSummary
from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.training import DailySolarTraining
from open_energy_planner.solar.wire import document
from tests.app.solar.boosting_support import runtime_case
from tests.app.solar.support import SITE, plant
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
    assert (
        learner.save(
            token, insufficient, {"model": None, "summary": document(insufficient.summary)}
        )
        == original
    )
    restored = DailySolarTraining(SolarStore(runtime.store.documents))
    assert restored.prepare(*arguments, now + timedelta(days=1, minutes=15))[0] == original
