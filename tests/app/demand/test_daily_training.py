"""Prove daily demand fitting is durable while forecasts and recovery remain responsive."""

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from open_energy_planner.appliances.forecasting import component
from open_energy_planner.appliances.weather_model import WeatherDemandModel
from open_energy_planner.demand.calculation.forecasting import (
    RidgeDemandModel,
    WeekdayProfile,
    predict,
)
from open_energy_planner.demand.calculation.model import (
    DemandConfiguration,
    DemandHour,
    EnergyTerm,
)
from open_energy_planner.demand.runtime import DemandRuntime
from open_energy_planner.demand.storage import DemandStore
from open_energy_planner.demand.training import daily_models
from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.storage.adapters.documents import SqlDocuments
from tests.app.demand.daily_training_support import NOW, samples_before, temperatures_for
from tests.database.support.archive import list_documents, make_store


@pytest.fixture
def demand_store(tmp_path):
    """Keep the real SQLite owner alive while tests reopen only the borrowed document reader."""
    return make_store(tmp_path, DemandStore)


@pytest.fixture
def configuration():
    """Bind the fit to one measured household source in the HA installation timezone."""
    return DemandConfiguration("Europe/Amsterdam", 3000, (EnergyTerm("sensor.house", 1),))


@pytest.fixture
def fit_counts():
    """Observe actual statistical learners rather than replacing their fitted coefficients."""
    with (
        patch.object(WeekdayProfile, "fit", wraps=WeekdayProfile.fit) as baseline,
        patch.object(RidgeDemandModel, "fit", wraps=RidgeDemandModel.fit) as ridge,
        patch.object(WeatherDemandModel, "fit", wraps=WeatherDemandModel.fit) as weather,
    ):
        yield baseline, ridge, weather


def pointer(documents):
    """Read the only fit family's durable outcome without depending on its hash construction."""
    pointers = list(documents.iter_prefix("active-training-"))
    assert len(pointers) == 1
    return pointers[0]


def test_same_day_weather_and_quarters_refresh_predictions_without_fitting(
    demand_store, configuration, fit_counts
):
    """Historical coefficients stay fixed while current weather changes the issued prediction."""
    samples = samples_before(NOW)
    weather = temperatures_for(samples, NOW)
    first = daily_models(
        demand_store.documents,
        configuration,
        samples,
        NOW,
        temperatures=weather,
        weather_sensitive=True,
        include_weather=False,
    )
    changed_weather = tuple(
        replace(row, celsius=row.celsius + 2) if row.start >= NOW else row for row in weather
    )
    repeated = daily_models(
        demand_store.documents,
        configuration,
        samples,
        NOW + timedelta(minutes=15),
        temperatures=changed_weather,
        weather_sensitive=True,
        include_weather=False,
    )
    assert first == repeated
    assert [learner.call_count for learner in fit_counts] == [1, 1, 0]
    original = predict(configuration, samples, NOW, first["ridge"], temperatures=weather)
    updated = predict(configuration, samples, NOW, repeated["ridge"], temperatures=changed_weather)
    assert original.weather_version != updated.weather_version
    assert original.steps != updated.steps


def test_restart_restores_all_three_native_free_model_archives(
    demand_store, configuration, fit_counts
):
    """A newly borrowed reader reconstructs baseline, Ridge and native weather predictions without fitting."""
    samples = samples_before(NOW)
    weather = temperatures_for(samples, NOW)
    first = daily_models(
        demand_store.documents,
        configuration,
        samples,
        NOW,
        temperatures=weather,
        weather_sensitive=True,
    )
    assert set(first) == {"baseline", "ridge", "weather"}
    reopened = SqlDocuments(demand_store.documents.engine, "demand")
    restored = daily_models(
        reopened,
        configuration,
        samples,
        NOW + timedelta(minutes=15),
        temperatures=weather,
        weather_sensitive=True,
    )
    assert restored == first
    assert [learner.call_count for learner in fit_counts] == [1, 1, 1]
    assert restored["weather"].predict(NOW, temperature=14) == pytest.approx(
        first["weather"].predict(NOW, temperature=14)
    )


def test_new_day_reuses_unchanged_data_and_refits_only_new_completed_data(
    demand_store, configuration, fit_counts
):
    """A calendar change alone creates no new weights, while a new complete measurement cohort does."""
    samples = samples_before(NOW)
    original = daily_models(demand_store.documents, configuration, samples, NOW)
    assert (
        daily_models(demand_store.documents, configuration, samples, NOW + timedelta(days=1))
        == original
    )
    assert [learner.call_count for learner in fit_counts] == [1, 1, 0]
    extended = samples + tuple(
        DemandHour(NOW + timedelta(hours=offset), 0.5, 0.5) for offset in range(24)
    )
    renewed = daily_models(demand_store.documents, configuration, extended, NOW + timedelta(days=1))
    assert [learner.call_count for learner in fit_counts] == [2, 2, 0]
    assert renewed["ridge"].trained_until == NOW + timedelta(days=1)
    assert original["ridge"].trained_until == NOW


@pytest.mark.parametrize("changed", ["source", "configuration", "boundary"])
def test_configuration_or_measurement_boundary_changes_fit_immediately(
    demand_store, configuration, fit_counts, changed
):
    """Another source, declared household or residual composition cannot borrow the previous fit."""
    samples = samples_before(NOW)
    daily_models(demand_store.documents, configuration, samples, NOW, boundary="original")
    altered = (
        replace(configuration, terms=(EnergyTerm("sensor.other", 1),))
        if changed == "source"
        else replace(configuration, annual_kwh=3500)
        if changed == "configuration"
        else configuration
    )
    daily_models(
        demand_store.documents,
        altered,
        samples,
        NOW + timedelta(minutes=15),
        boundary="changed" if changed == "boundary" else "original",
    )
    assert [learner.call_count for learner in fit_counts] == [2, 2, 0]
    assert len(list(demand_store.documents.iter_prefix("active-training-"))) == 1


def test_completed_current_day_samples_are_available_but_future_labels_are_not(
    demand_store, configuration, fit_counts
):
    """Bootstrap uses today's already completed hours and ignores changes to future measurements."""
    samples = samples_before(NOW, hours=7 * 24)
    future = DemandHour(NOW + timedelta(hours=1), 99, 99)
    first = daily_models(demand_store.documents, configuration, samples + (future,), NOW)
    assert first["baseline"].residual_profile
    assert fit_counts[0].call_args.args[1] == samples
    second = daily_models(
        demand_store.documents,
        configuration,
        samples + (replace(future, total_kwh=88, residual_kwh=88),),
        NOW + timedelta(minutes=15),
    )
    assert first == second
    assert fit_counts[0].call_count == 1


def test_insufficient_new_day_fit_retains_model_and_recovers_on_repaired_input(
    demand_store, configuration, monkeypatch
):
    """An older valid fit remains usable and does not falsely close the new day's recovery gate."""
    samples = samples_before(NOW)
    previous = daily_models(demand_store.documents, configuration, samples, NOW)
    next_day = NOW + timedelta(days=1)
    extended = samples + samples_before(next_day, hours=24)
    original_fit = RidgeDemandModel.fit
    with patch.object(RidgeDemandModel, "fit", side_effect=ValueError("Synthetic missing rows")):
        fallback = daily_models(demand_store.documents, configuration, extended, next_day)
    assert fallback["ridge"] == previous["ridge"]
    assert pointer(demand_store.documents)["complete"] is False
    with patch.object(RidgeDemandModel, "fit", wraps=original_fit) as fitting:
        daily_models(demand_store.documents, configuration, extended, next_day)
        assert fitting.call_count == 0
        repaired = (replace(extended[0], total_kwh=1, residual_kwh=1),) + extended[1:]
        recovered = daily_models(demand_store.documents, configuration, repaired, next_day)
        assert fitting.call_count == 1
    assert recovered["ridge"].trained_until == next_day
    assert pointer(demand_store.documents)["complete"] is True


def test_transient_failure_does_not_publish_or_replace_the_previous_pointer(
    demand_store, configuration
):
    """Native failure leaves the durable fit untouched and the following update can retry it."""
    samples = samples_before(NOW)
    previous = daily_models(demand_store.documents, configuration, samples, NOW)
    before = pointer(demand_store.documents)
    next_day = NOW + timedelta(days=1)
    extended = samples + samples_before(next_day, hours=24)
    with patch.object(
        RidgeDemandModel, "fit", side_effect=RuntimeError("Synthetic unavailable fit")
    ):
        fallback = daily_models(demand_store.documents, configuration, extended, next_day)
    assert fallback["ridge"] == previous["ridge"]
    assert pointer(demand_store.documents) == before
    restored = daily_models(demand_store.documents, configuration, extended, next_day)
    assert restored["ridge"].trained_until == next_day
    assert pointer(demand_store.documents) != before


def test_native_training_failure_keeps_new_scalar_fit_during_retry(demand_store, configuration):
    """A baseline-only row permits a new Ridge fit when the optional weather trainer fails."""
    short_history = samples_before(NOW, hours=7 * 24)
    documents = demand_store.documents
    first = daily_models(documents, configuration, short_history, NOW, weather_sensitive=True)
    assert set(first) == {"baseline"}
    before = pointer(documents)
    later = NOW + timedelta(days=1)
    samples = samples_before(later)
    with patch.object(
        WeatherDemandModel, "fit", side_effect=RuntimeError("Native learner unavailable")
    ):
        usable = daily_models(
            documents,
            configuration,
            samples,
            later,
            temperatures=temperatures_for(samples, later),
            weather_sensitive=True,
        )
    assert set(usable) == {"baseline", "ridge"}
    assert usable["ridge"].trained_until == later
    assert pointer(documents) == before


@pytest.mark.parametrize("learner", [RidgeDemandModel, WeatherDemandModel])
def test_transient_training_diagnostics_deduplicate_and_recover_without_private_payload(
    demand_store, configuration, caplog, learner
):
    """An independent learner warns once, retries quietly and reports actual recovery safely."""
    samples = samples_before(NOW)
    weather = temperatures_for(samples, NOW)
    arguments = {"temperatures": weather, "weather_sensitive": True}
    daily_models(demand_store.documents, configuration, samples, NOW, **arguments)
    next_day = NOW + timedelta(days=1)
    extended = samples + samples_before(next_day, hours=24)
    arguments["temperatures"] = temperatures_for(extended, next_day)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="open_energy_planner.demand.training"):
        with patch.object(learner, "fit", side_effect=RuntimeError("PRIVATE_TRAINING_PAYLOAD")):
            for _ in range(2):
                daily_models(demand_store.documents, configuration, extended, next_day, **arguments)
        daily_models(demand_store.documents, configuration, extended, next_day, **arguments)
    kind = "ridge" if learner is RidgeDemandModel else "weather"
    records = [
        record for record in caplog.records if f"Daily demand {kind} training" in record.message
    ]
    assert [record.levelno for record in records] == [logging.WARNING, logging.DEBUG, logging.INFO]
    assert "recovered" in records[-1].message
    assert "PRIVATE_TRAINING_PAYLOAD" not in caplog.text
    assert all(record.exc_info is None for record in records)


def test_interrupted_publication_rolls_back_new_weights_and_pointer(
    demand_store, configuration, monkeypatch
):
    """A failed write leaves the previous active models intact."""
    samples = samples_before(NOW)
    daily_models(demand_store.documents, configuration, samples, NOW)
    before = pointer(demand_store.documents)
    original_write = demand_store.documents.write

    def interrupt(name, value):
        """Interrupt publication after the candidate models have been fitted and validated."""
        if name.startswith("active-training-"):
            raise OSError("Synthetic interrupted daily model publication")
        original_write(name, value)

    monkeypatch.setattr(demand_store.documents, "write", interrupt)
    next_day = NOW + timedelta(days=1)
    with pytest.raises(OSError):
        daily_models(
            demand_store.documents,
            configuration,
            samples + samples_before(next_day, hours=24),
            next_day,
        )
    assert pointer(demand_store.documents) == before


def test_corrupt_pointer_fails_closed_instead_of_retraining_over_original_evidence(
    tmp_path, demand_store, configuration, fit_counts
):
    """An impossible future pointer is reported without silently replacing its archived model."""
    samples = samples_before(NOW)
    daily_models(demand_store.documents, configuration, samples, NOW)
    name = next(
        name for name in list_documents(tmp_path, "demand") if name.startswith("active-training-")
    )
    corrupt = {**demand_store.documents.read(name), "day": "2100-01-01"}
    demand_store.documents.write(name, corrupt)
    with pytest.raises(ValueError):
        daily_models(demand_store.documents, configuration, samples, NOW)
    assert [learner.call_count for learner in fit_counts] == [1, 1, 0]
    assert demand_store.documents.read(name) == corrupt


@pytest.mark.parametrize(
    "moments",
    [
        ("2026-03-28T22:30:00+00:00", "2026-03-28T23:15:00+00:00", "2026-03-29T22:15:00+00:00"),
        ("2026-10-24T21:30:00+00:00", "2026-10-24T22:15:00+00:00", "2026-10-25T23:15:00+00:00"),
    ],
)
def test_daily_schedule_uses_ha_calendar_across_short_and_long_dst_days(
    demand_store, configuration, fit_counts, moments
):
    """New complete data retrains at HA local midnight, including 23-hour and 25-hour days."""
    times = tuple(datetime.fromisoformat(value) for value in moments)
    initial = samples_before(times[0])
    daily_models(demand_store.documents, configuration, initial, times[0])
    for instant in times[1:]:
        daily_models(demand_store.documents, configuration, samples_before(instant), instant)
    assert [learner.call_count for learner in fit_counts] == [3, 3, 0]
    assert pointer(demand_store.documents)["day"] == (
        "2026-03-30" if moments[0].startswith("2026-03") else "2026-10-26"
    )


def test_repeated_dst_hour_is_one_training_day(demand_store, configuration, fit_counts):
    """The two different UTC instants called 02:30 locally reuse one frozen autumn-day fit."""
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    second = first + timedelta(hours=1)
    daily_models(demand_store.documents, configuration, samples_before(first), first)
    daily_models(demand_store.documents, configuration, samples_before(second), second)
    assert [learner.call_count for learner in fit_counts] == [1, 1, 0]


def test_cached_component_cannot_replace_missing_current_measurement_coverage(
    demand_store, configuration, fit_counts
):
    """Losing a component's accepted source history still blocks separation after models were saved."""
    component(configuration, samples_before(NOW), (), NOW, False, documents=demand_store.documents)
    with pytest.raises(ValueError):
        component(
            configuration,
            (),
            (),
            NOW + timedelta(minutes=15),
            False,
            documents=demand_store.documents,
        )
    assert [learner.call_count for learner in fit_counts] == [1, 1, 0]


def household_runtime(store, configuration):
    """Provide a real household runtime with forty completed measured days and no selected devices."""
    store.save(configuration)
    runtime = DemandRuntime(store)
    samples = samples_before(NOW)
    total = 100
    rows = [EnergyHour(samples[0].start - timedelta(hours=1), total, total)]
    for sample in samples:
        total += sample.residual_kwh
        rows.append(EnergyHour(sample.start, total, total))
    runtime.histories["sensor.house"] = EnergyHistory("sensor.house", tuple(rows))
    runtime.data_versions["sensor.house"] = "complete"
    return runtime


def test_household_runtime_uses_one_daily_binding_without_weather_candidate(
    demand_store, configuration, fit_counts
):
    """Household selection and repeated quarters reuse the same baseline and Ridge fit policy."""
    runtime = household_runtime(demand_store, configuration)
    for instant in (NOW, NOW + timedelta(minutes=15)):
        assert runtime.install(runtime.build(runtime.job(instant)), instant)
    assert [learner.call_count for learner in fit_counts] == [1, 1, 0]
    assert len(list(demand_store.documents.iter_prefix("active-training-"))) == 1
    assert runtime.forecast.model == "weekday-hour-v2"


@pytest.mark.parametrize("remaining_hours,scorer", [(144, False), (0, False), (0, True)])
def test_household_runtime_keeps_cached_baseline_when_current_history_shrinks(
    demand_store, configuration, fit_counts, remaining_hours, scorer
):
    """Reduced current evidence blocks fresh selection without discarding an existing household model."""
    runtime = household_runtime(demand_store, configuration)
    assert runtime.install(runtime.build(runtime.job(NOW)), NOW)
    original = runtime.forecast
    if remaining_hours:
        history = runtime.histories["sensor.house"]
        runtime.histories["sensor.house"] = replace(
            history, hours=history.hours[-(remaining_hours + 1) :]
        )
    else:
        runtime.histories.clear()
    if scorer:
        runtime.scorer = lambda *_args: pytest.fail(
            "Empty current history must not invoke a scorer"
        )
    else:
        runtime.learned_preferred = True
    later = NOW + timedelta(minutes=15)
    assert runtime.install(runtime.build(runtime.job(later)), later)
    assert runtime.forecast.model == original.model == "weekday-hour-v2"
    assert sum(step.kwh for step in runtime.forecast.steps) == pytest.approx(
        sum(step.kwh for step in original.steps)
    )
    assert runtime.daily_selection is None
    assert [learner.call_count for learner in fit_counts] == [1, 1, 0]
