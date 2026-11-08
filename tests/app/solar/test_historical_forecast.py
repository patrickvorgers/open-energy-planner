"""Prove original-forecast training, deterministic replay and immediate evidence-based selection."""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.solar.backtest import HistoricalForecastAssessment, scheduled_runs
from open_energy_planner.solar.calculation.model import SolarWeatherHour, version
from open_energy_planner.solar.forecast_weather import ForecastWeatherRun
from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.wire import document
from tests.app.solar.support import SITE, plant
from tests.database.support.archive import make_store

NOW = datetime(2026, 8, 20, tzinfo=UTC)


def original_forecasts(now=NOW, days=100):
    """Create independent immutable daily runs with a known recurring metered production pattern."""
    start = now - timedelta(days=days)
    runs = []
    for day in range(days):
        initialized = start + timedelta(days=day, hours=12)
        hours = []
        for index in range(95):
            instant = initialized + timedelta(hours=index)
            lit = 8 <= instant.hour < 16
            hours.append(SolarWeatherHour(instant, 600 * lit, 500 * lit, 100 * lit, 20, 2, now))
        runs.append(
            ForecastWeatherRun(
                initialized_at=initialized,
                received_at=now,
                location_version=version(asdict(SITE)),
                hours=tuple(hours),
            )
        )
    total, readings = 100.0, []
    for hour in range(-1, days * 24):
        instant = start + timedelta(hours=hour)
        if 8 <= instant.hour < 16:
            total += 0.24 if instant.hour < 11 else 0.864
        readings.append(EnergyHour(instant, total, total))
    return tuple(runs), EnergyHistory("sensor.pv0", tuple(readings))


def configured_runtime(tmp_path, *, runs=None, history=None):
    """Use actual app orchestration and native models, with synthetic provider and Recorder inputs."""
    supplied_runs, supplied_history = original_forecasts()
    runs = supplied_runs if runs is None else runs
    history = supplied_history if history is None else history
    store = make_store(tmp_path, SolarStore)
    store.save(plant())
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, supplied_runs[-1].hours)
    runtime.histories["sensor.pv0"] = history
    runtime.set_forecast_runs(SITE, runs)
    return runtime


def test_original_forecast_backtest_selects_ml_without_collecting_three_new_days(tmp_path):
    """Four old weekly runs prove both horizons and activate ML immediately with zero live captured days."""
    runtime = configured_runtime(tmp_path)
    result = runtime.build(runtime.job(NOW))
    assert runtime.install(result, NOW)
    report = runtime.learning["pv0"].summary.historical_assessment
    assert report is not None and report.status == "eligible"
    assert report.matched_pairs == 384 and report.unique_hours == 288
    assert [score.matched_hours for score in report.per_origin] == [192, 192]
    assert len(report.windows) == 8
    assert all(
        window.training_end <= window.origin and window.model_created_at == NOW
        for window in report.windows
    )
    assert all(
        window.origin == window.initialized_at + timedelta(hours=window.origin_delay_hours)
        for window in report.windows
    )
    assert runtime.source_selection["pv0"]["active"] == "ml"
    assert runtime.source_selection["pv0"]["admission_basis"] == "historical_forecast_backtest"
    assert runtime.source_selection["pv0"]["complete_days"] == 0
    assert runtime.source_selection["pv0"]["required_days"] == 0
    assert runtime.active_model("pv0") == "solar-xgboost-production-v2"
    assert runtime.forecast.energies == runtime.candidate.energies
    assert HistoricalForecastAssessment.restore(document(report)) == report
    public = runtime.document(NOW)
    assert (
        public["learning"]["pv0"]["training_samples"] > public["learning"]["pv0"]["training_hours"]
    )
    assert "model_json" not in str(public)
    withdrawn = scheduled_runs(NOW)[0]
    runtime.set_forecast_runs(
        SITE, tuple(run for run in runtime.forecast_runs if run.initialized_at != withdrawn)
    )
    assert runtime.forecast == runtime.baseline
    assert runtime.candidate is None
    assert runtime.source_selection == runtime.learning == {}
    assert runtime.active_model("pv0") == "pvwatts-v2"
    assert not runtime.install(result, NOW)


def test_observed_weather_cannot_substitute_for_missing_original_runs(tmp_path):
    """Years of generic weather and measurements cannot manufacture original forecast evidence."""
    runtime = configured_runtime(tmp_path, runs=())
    assert runtime.install(runtime.build(runtime.job(NOW)), NOW)
    assert runtime.learning["pv0"].model is None
    assert runtime.learning["pv0"].summary.training_hours == 0
    assert runtime.source_selection["pv0"]["active"] == "pvwatts"


def test_missing_deterministic_run_is_not_replaced_by_a_more_favorable_available_day(tmp_path):
    """A hole in a predetermined window prevents admission even though other old runs train a model."""
    runs, history = original_forecasts()
    missing = scheduled_runs(NOW)[1]
    runtime = configured_runtime(
        tmp_path, runs=tuple(run for run in runs if run.initialized_at != missing), history=history
    )
    assert runtime.install(runtime.build(runtime.job(NOW)), NOW)
    report = runtime.learning["pv0"].summary.historical_assessment
    assert report.status == "insufficient_history"
    assert report.run_initializations == scheduled_runs(NOW)
    assert runtime.source_selection["pv0"]["active"] == "pvwatts"


def test_stored_historical_summary_cannot_fake_a_pass_or_the_unique_hour_count(tmp_path):
    """Replaying JSON recomputes derived eligibility and rejects tampered evidence summaries."""
    runtime = configured_runtime(tmp_path)
    result = runtime.build(runtime.job(NOW))
    report = result[-1]["pv0"].summary.historical_assessment
    body = document(report)
    body["unique_hours"] = 384
    with pytest.raises(ValueError, match="Invalid historical solar forecast evidence"):
        HistoricalForecastAssessment.restore(body)


def test_four_windows_are_fixed_before_any_outcome_and_end_before_recorder_lag():
    """Weekly origins are calendar policy, not a subset selected from favorable measured days."""
    assert scheduled_runs(NOW) == (
        datetime(2026, 7, 25, 12, tzinfo=UTC),
        datetime(2026, 8, 1, 12, tzinfo=UTC),
        datetime(2026, 8, 8, 12, tzinfo=UTC),
        datetime(2026, 8, 15, 12, tzinfo=UTC),
    )


def test_both_simulated_origins_and_raw_model_must_improve(tmp_path):
    """A good served average cannot hide a bad raw learner or a losing day-ahead horizon."""
    runtime = configured_runtime(tmp_path)
    assert runtime.install(runtime.build(runtime.job(NOW)), NOW)
    report = runtime.learning["pv0"].summary.historical_assessment
    for raw_only in (False, True):
        windows = tuple(
            replace(
                window,
                model_mae_kwh=window.baseline_mae_kwh,
                **({} if raw_only else {"service_mae_kwh": window.baseline_mae_kwh}),
            )
            if window.origin_delay_hours == 24
            else window
            for window in report.windows
        )
        changed = replace(report, windows=windows)
        assert changed.status == "not_improved"
        assert changed.matched_pairs == 384 and changed.unique_hours == 288


def test_original_forecasts_not_reanalysis_determine_native_training(tmp_path):
    """Changing unrelated observed-weather input cannot change forecast-trained weights or replay evidence."""
    runtime = configured_runtime(tmp_path)
    assert runtime.install(runtime.build(runtime.job(NOW)), NOW)
    original = runtime.learning["pv0"]
    runtime.set_weather(
        SITE, tuple(replace(hour, ghi=0, dni=0, dhi=0, celsius=-10) for hour in runtime.weather)
    )
    assert runtime.install(runtime.build(runtime.job(NOW)), NOW)
    changed = runtime.learning["pv0"]
    assert original.model == changed.model
    assert original.summary.historical_assessment == changed.summary.historical_assessment


def test_historical_evidence_survives_restart_and_is_reused_without_retraining(
    tmp_path, monkeypatch
):
    """A fresh active-store reader reuses exact retained evidence rather than recalculating old origins."""
    from open_energy_planner.solar import boosting
    from open_energy_planner.solar.backtest import assess_forecasts

    runtime = configured_runtime(tmp_path)
    assert runtime.install(runtime.build(runtime.job(NOW)), NOW)
    result = runtime.learning["pv0"]

    def unexpected_training(*arguments, **keywords):
        """Fail if a cache hit attempts to train another native model."""
        raise AssertionError("Completed historical evidence must be reused")

    monkeypatch.setattr(boosting, "fit_boosted_model", unexpected_training)
    restored = SolarStore(runtime.store.documents)
    replay = assess_forecasts(
        runtime.configuration,
        runtime.configuration.sources[0],
        SITE,
        runtime.histories["sensor.pv0"],
        runtime.forecast_runs,
        NOW,
        result.model.policy_version,
        restored,
        histories=runtime.histories,
    )
    assert replay == result.summary.historical_assessment


def test_all_actual_training_outcomes_end_before_each_simulated_origin(tmp_path, monkeypatch):
    """Real native fold inputs contain no later labels even when many forecast runs overlap an hour."""
    from open_energy_planner.solar import boosting

    original_fit, original_train = boosting.fit_boosted_model, boosting._train
    cutoff, seen = [None], []

    def capture_fit(*arguments, **keywords):
        """Track the actual outer cutoff passed to the native fitting path."""
        cutoff[0] = arguments[6]
        return original_fit(*arguments, **keywords)

    def capture_train(rows):
        """Check every actual target endpoint rather than trusting reported training timestamps."""
        seen.append((max(row[0] for row in rows) + timedelta(hours=1), cutoff[0]))
        return original_train(rows)

    monkeypatch.setattr(boosting, "fit_boosted_model", capture_fit)
    monkeypatch.setattr("open_energy_planner.solar.runtime.fit_boosted_model", capture_fit)
    monkeypatch.setattr(boosting, "_train", capture_train)
    monkeypatch.setattr(boosting, "_choose_trend", boosting._choose_trend.__wrapped__)
    runtime = configured_runtime(tmp_path)
    assert runtime.install(runtime.build(runtime.job(NOW)), NOW)
    assert len(seen) >= 9
    assert all(endpoint <= origin for endpoint, origin in seen)


def test_a_physically_invalid_holdout_reading_cannot_establish_forecast_quality(tmp_path):
    """A meter spike inside a predetermined test window blocks that cohort rather than favoring a model."""
    runs, history = original_forecasts()
    fault = scheduled_runs(NOW)[0] + timedelta(hours=2)
    damaged = replace(
        history,
        hours=tuple(
            replace(
                hour,
                total_kwh=hour.total_kwh + (100 if hour.start >= fault else 0),
                meter_kwh=hour.meter_kwh + (100 if hour.start >= fault else 0),
            )
            for hour in history.hours
        ),
    )
    runtime = configured_runtime(tmp_path, runs=runs, history=damaged)
    assert runtime.install(runtime.build(runtime.job(NOW)), NOW)
    report = runtime.learning["pv0"].summary.historical_assessment
    assert report.status == "insufficient_history"
    assert runtime.source_selection["pv0"]["active"] == "pvwatts"
    assert runtime.histories["sensor.pv0"] == damaged


def test_a_new_learning_regime_cannot_reuse_the_old_historical_admission(tmp_path):
    """Changing the declared installation regime invalidates old proof even when its windows remain recent."""
    from open_energy_planner.solar.source_selection import choose_sources

    runtime = configured_runtime(tmp_path)
    assert runtime.install(runtime.build(runtime.job(NOW)), NOW)
    result = runtime.learning["pv0"]
    changed = replace(
        runtime.configuration,
        sources=(
            replace(
                runtime.configuration.sources[0],
                learning_start=(NOW - timedelta(days=70)).isoformat(),
            ),
        ),
    )
    baseline = replace(runtime.baseline, configuration_version=changed.version)
    candidate = replace(runtime.candidate, configuration_version=changed.version)
    _choices, status = choose_sources(
        runtime.store,
        changed,
        baseline,
        candidate,
        (result.model,),
        {},
        runtime.histories,
        NOW,
        ml_policies={"pv0": result.model.policy_version},
        historical_assessments={"pv0": result.summary.historical_assessment},
    )
    assert status["pv0"]["active"] == "pvwatts"
    assert status["pv0"]["admission_basis"] == "captured_forecasts"


def test_shared_inverter_geometry_invalidates_proof_but_an_independent_inverter_does_not():
    """A second plane halves clipped yield on a shared converter, while a separate converter leaves it unchanged."""
    from open_energy_planner.solar.backtest import evidence_family, physical_run
    from open_energy_planner.solar.calculation.model import SolarInverter

    original = plant()
    original = replace(original, inverters=(replace(original.inverters[0], ac_limit_w=100),))
    source = original.sources[0]
    second = replace(source, source_id="pv1", entity_id="sensor.pv1")
    shared = replace(original, sources=(source, second))
    independent = replace(
        original,
        sources=(source, replace(second, inverter_id="separate")),
        inverters=(*original.inverters, SolarInverter("separate", 1000)),
    )
    run = original_forecasts()[0][-1]
    single_peak = max(
        row.ac_kwh for row in physical_run(original, SITE, run) if row.source_id == "pv0"
    )
    shared_peak = max(
        row.ac_kwh for row in physical_run(shared, SITE, run) if row.source_id == "pv0"
    )
    independent_peak = max(
        row.ac_kwh for row in physical_run(independent, SITE, run) if row.source_id == "pv0"
    )
    assert single_peak == pytest.approx(0.1)
    assert shared_peak == pytest.approx(0.05)
    assert independent_peak == pytest.approx(0.1)
    identity = evidence_family(original, source, SITE, "a" * 64)
    assert evidence_family(shared, source, SITE, "a" * 64) != identity
    assert evidence_family(independent, source, SITE, "a" * 64) == identity


def test_historical_report_cannot_claim_a_different_active_learning_policy(tmp_path):
    """A tested model cannot authorize an active predictor with another feature, weather or fitting policy."""
    from open_energy_planner.solar.backtest import assess_forecasts

    runtime = configured_runtime(tmp_path)
    with pytest.raises(ValueError, match="Historical solar learning policy changed"):
        assess_forecasts(
            runtime.configuration,
            runtime.configuration.sources[0],
            SITE,
            runtime.histories["sensor.pv0"],
            runtime.forecast_runs,
            NOW,
            "0" * 64,
            runtime.store,
            histories=runtime.histories,
        )


@pytest.mark.parametrize("change", ["removed", "replaced"])
def test_withdrawn_or_replaced_forecast_cohort_rejects_captured_worker(
    tmp_path, monkeypatch, change
):
    """A delayed worker cannot publish after any of its original forecast evidence is withdrawn."""
    runs, _history = original_forecasts(days=5)
    runtime = configured_runtime(tmp_path, runs=runs[-2:])
    monkeypatch.setattr(runtime, "_learned_candidates", lambda *arguments: {})
    result = runtime.build(runtime.job(NOW))
    assert runtime.install(result, NOW)
    original = runtime.forecast
    if change == "removed":
        newer = runtime.forecast_runs[1:]
    else:
        replaced = runtime.forecast_runs[0]
        newer = (
            replace(replaced, hours=(replace(replaced.hours[0], celsius=21), *replaced.hours[1:])),
            runtime.forecast_runs[1],
        )
    assert runtime.set_forecast_runs(SITE, newer)
    assert not runtime.install(result, NOW)
    assert runtime.forecast == runtime.baseline == original
    assert runtime.last_key is None
    assert runtime.source_selection == runtime.learning == {}
    following = runtime.job(NOW)
    assert following is not None and following[7] == runtime.forecast_runs
    assert runtime.install(runtime.build(following), NOW)


def test_additional_runs_preserve_a_worker_that_captured_a_nonempty_valid_cohort(
    tmp_path, monkeypatch
):
    """Importing older immutable runs queues a later fit without starving the valid active worker."""
    runs, _history = original_forecasts(days=5)
    runtime = configured_runtime(tmp_path, runs=runs[-2:])
    monkeypatch.setattr(runtime, "_learned_candidates", lambda *arguments: {})
    queued = runtime.job(NOW)
    result = runtime.build(queued)
    assert runtime.set_forecast_runs(SITE, runs)
    assert runtime.install(result, NOW)
    assert runtime.last_key == queued[0]
    assert runtime.job(NOW + timedelta(seconds=30)) is None
    following = runtime.job(NOW + timedelta(seconds=60))
    assert following is not None and following[7] == runtime.forecast_runs


@pytest.mark.parametrize("missing", ["all", "one", "none"])
def test_historical_projection_requires_coverage_but_keeps_explicit_zeros(missing):
    """The real projection rejects absent weather hours while retaining a complete zero-yield horizon."""
    from open_energy_planner.solar.calculation.projection import learned_forecast

    class ZeroProductionModel:
        """Supply explicit accepted zero production for every provided weather interval."""

        version = "synthetic-zero-production"

        def predict(self, weather, location, *, known_at, duration_h, offset_minutes):
            """Keep missing weather absent and provide valid zeros for supplied hours, including night."""
            return {
                row.start + timedelta(minutes=offset_minutes): 0.0
                for row in weather
                if row.received_at <= known_at
            }

    hours = tuple(
        SolarWeatherHour(NOW + timedelta(hours=index), 0, 0, 0, 10, 1, NOW) for index in range(49)
    )
    supplied = () if missing == "all" else hours[:17] + hours[18:] if missing == "one" else hours
    if missing != "none":
        with pytest.raises(ValueError, match="solar_weather_coverage_missing"):
            learned_forecast(plant(), SITE, supplied, NOW, {"pv0": ZeroProductionModel()})
    else:
        forecast = learned_forecast(plant(), SITE, supplied, NOW, {"pv0": ZeroProductionModel()})
        assert len(forecast.energies) == 120
        assert sum(row.duration_h for row in forecast.energies) == 48
        assert {row.start.replace(minute=0) for row in forecast.energies} == {
            NOW + timedelta(hours=index) for index in range(48)
        }
        assert all(row.ac_kwh == row.dc_kwh == 0 for row in forecast.energies)
