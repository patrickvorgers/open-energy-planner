"""Learn repeated solar patterns without changing raw measurements or admitting future outcomes."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.solar.boosting import SolarBoostedModel, fit_boosted_model
from open_energy_planner.solar.calculation.model import SolarEnergy, SolarWeatherHour
from open_energy_planner.solar.curtailment import CurtailmentHistory
from open_energy_planner.solar.trend import YieldTrend
from open_energy_planner.solar.wire import document
from tests.app.solar.support import SITE, plant

START = datetime(2026, 4, 1, tzinfo=UTC)


def test_trend_archive_replays_hourly_and_quarterly_forecasts_after_restart():
    """Restored trend parameters scale native prediction identically at hourly and fine intervals."""
    result, _history, _physical, _weather, before = fit_case()
    trend = YieldTrend(-0.01, before - timedelta(days=365))
    changed = replace(result.model, time_trend=trend)
    restored = SolarBoostedModel.restore(document(changed))
    weather = (SolarWeatherHour(before + timedelta(hours=12), 600, 500, 100, 20, 2, before),)
    for duration, offset in ((1, 0), (0.25, 15)):
        plain = result.model.predict(
            weather, SITE, known_at=before, duration_h=duration, offset_minutes=offset
        )
        replay = restored.predict(
            weather, SITE, known_at=before, duration_h=duration, offset_minutes=offset
        )
        assert replay == changed.predict(
            weather, SITE, known_at=before, duration_h=duration, offset_minutes=offset
        )
        assert all(
            replay[instant] == pytest.approx(value * trend.factor(instant))
            for instant, value in plain.items()
        )
    assert restored.version != result.model.version


@pytest.mark.parametrize(
    "changes",
    (
        {"log_rate": -1},
        {"log_rate": True},
        {"reference": "2026-12-31T00:00:00+00:00"},
        {"reference": "2024-01-01T00:00:00"},
    ),
)
def test_invalid_trend_archives_do_not_reach_prediction(changes):
    """Oversized rates, naive references and references beyond training are rejected on restart."""
    result, *_ = fit_case()
    body = document(result.model)
    body["time_trend"] = {"log_rate": -0.01, "reference": "2024-01-01T00:00:00+00:00", **changes}
    with pytest.raises(ValueError):
        SolarBoostedModel.restore(body)


def training_data(*, constant=None, corrupt=False, reverse_validation=False):
    """Specify 56 days with three shaded morning hours and five unshaded production hours."""
    end = START + timedelta(days=56)
    readings, physical, weather = [], [], []
    total = 100.0
    for index in range(-1, 56 * 24):
        instant = START + timedelta(hours=index)
        daylight = 8 <= instant.hour < 16
        ratio = constant if constant is not None else 0.25 if instant.hour < 11 else 0.9
        actual = ratio * 0.96 if daylight else 0.0
        if reverse_validation and instant >= end - timedelta(days=7):
            actual = 0.0
        if corrupt and instant == START + timedelta(days=10, hours=12):
            actual = 50.0
        if corrupt and instant == START + timedelta(days=15, hours=9):
            actual = 0.0
        total += actual
        readings.append(EnergyHour(instant, total, total % 20))
        physical.append(SolarEnergy("pv0", instant, 1, float(daylight), 0.96 * daylight))
        weather.append(SolarWeatherHour(instant, 600, 500, 100, 20, 2, end))
    return EnergyHistory("sensor.pv0", tuple(readings)), tuple(physical), tuple(weather), end


def fit_case(**changes):
    """Fit one independently metered standalone source against a separately specified linear prior."""
    configuration = plant()
    history, physical, weather, before = training_data(**changes)
    result = fit_boosted_model(
        configuration.sources[0],
        configuration.inverters[0],
        SITE,
        history,
        physical,
        weather,
        before,
    )
    return result, history, physical, weather, before


@pytest.mark.parametrize("linked", [False, True])
def test_switchable_source_can_train_before_its_first_feedback_observation(linked):
    """Actual nonlinear fitting uses older production both without a link and before a later first feedback reading."""
    configuration = plant()
    history, physical, weather, before = training_data()
    entity = "binary_sensor.curtailed" if linked else None
    source = replace(configuration.sources[0], can_curtail=True, curtailment_entity_id=entity)
    feedback = (
        CurtailmentHistory(
            entity, before + timedelta(hours=2), ((before + timedelta(hours=1), True),)
        )
        if linked
        else None
    )
    result = fit_boosted_model(
        source,
        configuration.inverters[0],
        SITE,
        history,
        physical,
        weather,
        before,
        feedback=feedback,
    )
    assert result.model is not None
    assert result.summary.training_days >= 28
    assert result.summary.validation_days == 7
    assert result.summary.excluded_counts.get("control_history_unconfirmed", 0) == 0


def test_valid_daylight_production_is_not_rejected_by_a_zero_physical_prediction():
    """A zero PVWatts estimate cannot discard measured daylight targets or their validation days."""
    configuration = plant()
    history, physical, weather, before = training_data()
    physical = tuple(replace(row, dc_kwh=0, ac_kwh=0) for row in physical)
    result = fit_boosted_model(
        configuration.sources[0],
        configuration.inverters[0],
        SITE,
        history,
        physical,
        weather,
        before,
    )
    assert result.summary.training_hours >= 392
    assert result.summary.validation_hours >= 56
    assert result.summary.excluded_counts.get("low_production", 0) == 0
    assert result.summary.excluded_counts.get("production_out_of_range", 0) == 0


def test_direct_production_pattern_beats_physics_and_replays_json():
    """Unseen daylight hours retain measured 0.24/0.864 kWh without a physical multiplier."""
    result, _history, _physical, _weather, before = fit_case()
    assert result.model is not None
    assert result.summary.status == "nonlinear_shadow"
    assert result.summary.validation_hours >= 56
    assert result.summary.tree_mae_kwh < result.summary.baseline_mae_kwh * 0.2
    physical = tuple(
        SolarEnergy("pv0", before + timedelta(hours=hour), 1, 1, 0.96) for hour in (9, 14)
    )
    weather = tuple(SolarWeatherHour(row.start, 600, 500, 100, 20, 2, before) for row in physical)
    restored = SolarBoostedModel.restore(document(result.model))
    predicted = restored.predict(weather, SITE, known_at=before)
    assert predicted[physical[0].start] == pytest.approx(0.24, abs=0.06)
    assert predicted[physical[1].start] == pytest.approx(0.864, abs=0.06)
    assert predicted == result.model.predict(weather, SITE, known_at=before)


def test_cleaning_retains_repeated_shade_and_original_counter_payloads():
    """One physically impossible jump is excluded while a valid isolated zero and recurring shade remain."""
    history, physical, weather, before = training_data(corrupt=True)
    original = deepcopy(history)
    configuration = plant()
    result = fit_boosted_model(
        configuration.sources[0],
        configuration.inverters[0],
        SITE,
        history,
        physical,
        weather,
        before,
    )
    assert history == original
    assert result.model is not None
    assert result.summary.excluded_counts["production_out_of_range"] == 1
    assert result.summary.excluded_counts.get("statistical_outlier", 0) == 0
    assert result.summary.training_hours >= 391
    assert result.summary.validation_hours >= 56


def test_correct_physics_does_not_promote_complexity_without_material_gain():
    """An exact physical baseline leaves no material held-out error for a tree model to improve."""
    result, *_ = fit_case(constant=1.0)
    assert result.model is None
    assert result.summary.status == "baseline_preferred"
    assert result.summary.baseline_mae_kwh < 0.005


def test_validation_outcomes_are_not_trimmed_to_make_trees_win():
    """A changed final week is scored as observed rather than excluded using training outlier fences."""
    result, *_ = fit_case(reverse_validation=True)
    assert result.summary.validation_hours >= 56
    assert result.summary.excluded_counts.get("statistical_outlier", 0) == 0


def test_model_and_weather_cannot_be_backdated_before_they_were_known():
    """Historical target cutoff cannot disguise a later model choice or a later weather receipt."""
    result, _history, _physical, _weather, before = fit_case()
    physical = (SolarEnergy("pv0", before + timedelta(hours=9), 1, 1, 0.96),)
    weather = (SolarWeatherHour(physical[0].start, 600, 500, 100, 20, 2, before),)
    with pytest.raises(ValueError, match="created after prediction"):
        result.model.predict(weather, SITE, known_at=before - timedelta(seconds=1))
    future_receipt = (replace(weather[0], received_at=before + timedelta(seconds=1)),)
    assert result.model.predict(future_receipt, SITE, known_at=before) == {}


@pytest.mark.parametrize(
    "damage",
    [
        "schema",
        "features",
        "cycle",
        "fractional_index",
        "checksum",
        "category_shape",
        "nonfinite",
        "metadata_time",
    ],
)
def test_restore_rejects_incompatible_or_unsafe_native_tree_data(damage):
    """Even a recomputed checksum cannot admit a cyclic tree, foreign features or invalid index types."""
    result, *_ = fit_case()
    body = document(result.model)
    native = json.loads(body["model_json"])
    if damage == "schema":
        body["schema"] = "unknown-model"
    elif damage == "features":
        native["learner"]["feature_names"].reverse()
    elif damage == "cycle":
        native["learner"]["gradient_booster"]["model"]["trees"][0]["left_children"][0] = 0
    elif damage == "category_shape":
        native["learner"]["gradient_booster"]["model"]["cats"] = None
    elif damage == "nonfinite":
        native["learner"]["gradient_booster"]["model"]["trees"][0]["split_conditions"][0] = float(
            "nan"
        )
    elif damage == "metadata_time":
        body["created_at"] = None
    elif damage == "fractional_index":
        native["learner"]["gradient_booster"]["model"]["trees"][0]["left_children"][0] = 1.5
    body["model_json"] = json.dumps(native)
    body["model_digest"] = hashlib.sha256(body["model_json"].encode()).hexdigest()
    if damage == "checksum":
        body["model_digest"] = "0" * 64
    with pytest.raises(ValueError):
        SolarBoostedModel.restore(body)


@pytest.mark.parametrize("curtail,hybrid,boundary", [(True, False, "ac"), (False, True, "ac")])
def test_learning_preserves_unknown_control_and_hybrid_measurement_guards(
    curtail, hybrid, boundary
):
    """More historical data cannot establish available production when its control or route is unknown."""
    configuration = plant()
    history, physical, weather, before = training_data()
    result = fit_boosted_model(
        replace(
            configuration.sources[0],
            can_curtail=curtail,
            measurement_boundary=boundary,
            curtailment_entity_id="binary_sensor.curtailed" if curtail else None,
        ),
        replace(configuration.inverters[0], hybrid=hybrid),
        SITE,
        history,
        physical,
        weather,
        before,
        feedback=CurtailmentHistory("binary_sensor.curtailed", before, ((START, True),))
        if curtail
        else None,
    )
    assert result.model is None
    if curtail:
        assert result.summary.status == "insufficient_clean_history"
        assert result.summary.excluded_counts["control_history_unconfirmed"] > 0
    else:
        assert result.summary.status == "blocked"
        assert result.summary.issue == "hybrid_route_history_required"


def runtime_case(tmp_path, *, store=None):
    """Build real geometry for two roofs, with repeated morning shade measured only on the first roof."""
    from open_energy_planner.solar.calculation.predictor import PVWattsPredictor
    from open_energy_planner.solar.runtime import SolarRuntime
    from open_energy_planner.solar.storage import SolarStore
    from tests.app.solar.support import START as NOW
    from tests.app.solar.support import weather
    from tests.database.support.archive import make_store

    configuration = plant(count=2)
    conditions = tuple(
        replace(row, received_at=NOW) for row in weather(NOW - timedelta(days=56), days=59)
    )
    physical = PVWattsPredictor().hourly(configuration, SITE, conditions)
    total, readings = 100.0, []
    for row in physical:
        if row.source_id == "pv0" and row.start < NOW:
            total += row.ac_kwh * (0.25 if row.start.hour < 11 else 0.9)
            readings.append(EnergyHour(row.start, total, total))
    store = store or make_store(tmp_path, SolarStore)
    store.save(configuration)
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, conditions)
    from tests.app.solar.support import frozen_runs

    runtime.set_forecast_runs(SITE, frozen_runs(conditions, SITE, NOW))
    runtime.histories["sensor.pv0"] = EnergyHistory("sensor.pv0", tuple(readings))
    return runtime, NOW


def test_runtime_archives_tree_and_cleaning_without_premature_activation(tmp_path):
    """A nonlinear roof has auditable native weights while its untrained neighbor and active PVWatts remain independent."""
    runtime, now = runtime_case(tmp_path)
    original = deepcopy(runtime.histories)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.histories == original
    result = runtime.learning["pv0"]
    assert result.model is not None
    assert runtime.candidate.model == "solar-xgboost-production-v2"
    assert runtime.forecast.model == "pvwatts-v2"
    assert runtime.source_selection["pv0"]["active"] == "pvwatts"
    models = runtime.store.documents.read(
        "quality-source-models-" + runtime.candidate.version + ".json"
    )
    assert models == {"pv0": "solar-xgboost-production-v2"}
    training = runtime.store.documents.read("ml-training-" + result.archive_reference + ".json")
    assert training["summary"]["validation_hours"] >= 28
    expected_history_digest = hashlib.sha256(
        json.dumps(document(original["sensor.pv0"]), sort_keys=True).encode()
    ).hexdigest()
    assert training["history_version"] == expected_history_digest
    weights = runtime.store.documents.read(
        "ml-weights-" + training["model"]["weights_reference"] + ".json"
    )
    assert "model_json" not in training["model"]
    restored = SolarBoostedModel.restore(
        {
            **{
                key: value for key, value in training["model"].items() if key != "weights_reference"
            },
            "model_json": weights["model_json"],
        }
    )
    assert restored == result.model
    public = runtime.document(now)["learning"]["pv0"]
    assert public["candidate_model"] == "solar-xgboost-production-v2"
    assert public["audit_reference"] == result.archive_reference
    assert "model_json" not in public and "excluded_periods" not in public
    before_weights = list(runtime.store.documents.iter_prefix("ml-weights-"))
    later = now + timedelta(minutes=15)
    assert runtime.install(runtime.build(runtime.job(later)), later)
    assert list(runtime.store.documents.iter_prefix("ml-weights-")) == before_weights


def test_optional_native_failure_retains_baseline_and_sanitizes_logs(tmp_path, monkeypatch, caplog):
    """An unavailable optional model cannot block physical forecasts or expose private native errors."""
    from open_energy_planner.solar import runtime as runtime_module

    runtime, now = runtime_case(tmp_path)

    def unavailable(*args, **kwargs):
        """Represent a native failure whose message is not safe for logs or API output."""
        raise RuntimeError("private-solar-location-and-provider-token")

    monkeypatch.setattr(runtime_module, "fit_boosted_model", unavailable)
    with caplog.at_level("DEBUG", logger="open_energy_planner.solar.runtime"):
        assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.forecast.model == "pvwatts-v2"
    assert runtime.candidate is None
    assert runtime.document(now)["learning"]["pv0"]["status"] == "training_failed"
    assert "private-solar-location-and-provider-token" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_native_failure_does_not_log_private_source_identity(tmp_path, monkeypatch, caplog):
    """A configured private identifier and exception values stay out of safe, useful diagnostics."""
    from open_energy_planner.solar import runtime as runtime_module

    runtime, now = runtime_case(tmp_path)
    private_id = "private-source-identity-sentinel"
    histories = dict(runtime.histories)
    configuration = replace(
        runtime.configuration,
        sources=(
            replace(runtime.configuration.sources[0], source_id=private_id),
            *runtime.configuration.sources[1:],
        ),
    )
    runtime.store.save(configuration)
    runtime.configure(configuration)
    runtime.histories = histories

    def unavailable(*args, **kwargs):
        """Keep a private provider value in the original exception, outside public diagnostics."""
        raise RuntimeError("private-provider-value-sentinel")

    monkeypatch.setattr(runtime_module, "fit_boosted_model", unavailable)
    with caplog.at_level("DEBUG", logger="open_energy_planner.solar.runtime"):
        assert runtime.install(runtime.build(runtime.job(now)), now)
    assert private_id not in caplog.text
    assert "private-provider-value-sentinel" not in caplog.text
    assert "Solar ML candidate" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "runtime.py:" in caplog.text and "unavailable" in caplog.text
    public = runtime.document(now)["learning"][private_id]
    assert public["issue"] == (
        "The ML candidate could not be updated. Simpler forecasts remain usable."
    )
    assert private_id not in public["issue"]


def test_native_prediction_retains_zero_night_and_independent_energy_units():
    """Midnight stays zero while afternoon output is measured kWh rather than a physical ratio."""
    result, *_rest, before = fit_case()
    conditions = tuple(
        SolarWeatherHour(before + timedelta(hours=hour), 600, 500, 100, 20, 2, before)
        for hour in (0, 9, 14)
    )
    predicted = result.model.predict(conditions, SITE, known_at=before)
    assert predicted[conditions[0].start] == 0
    assert predicted[conditions[1].start] == pytest.approx(0.24, abs=0.06)
    assert predicted[conditions[2].start] == pytest.approx(0.864, abs=0.06)


def test_direct_prediction_respects_physical_power_cap_and_quarter_energy_units():
    """A 0.2 kW physical cap allows at most 0.05 kWh in a quarter without consulting PVWatts."""
    result, *_rest, before = fit_case()
    bounded = replace(result.model, max_power_kw=0.2)
    conditions = (SolarWeatherHour(before + timedelta(hours=14), 600, 500, 100, 20, 2, before),)
    hourly = bounded.predict(conditions, SITE, known_at=before)
    quarter = bounded.predict(conditions, SITE, known_at=before, duration_h=0.25)
    assert hourly[conditions[0].start] == pytest.approx(0.2)
    assert quarter[conditions[0].start] == pytest.approx(0.05)


@pytest.mark.parametrize("learned_sources", [("pv0",), ("pv0", "pv1")])
def test_independent_sources_share_one_inverter_cap_without_borrowing_missing_models(
    learned_sources,
):
    """One 0.5 kW converter caps direct source outputs once while absent model sources stay absent."""
    from open_energy_planner.solar.calculation.projection import learned_forecast
    from tests.app.solar.support import START as NOW
    from tests.app.solar.support import weather

    class BoundaryModel:
        """Produce an independently fixed 0.8 kW daytime estimate at the metered boundary."""

        version = "0" * 64

        def predict(self, conditions, location, *, known_at, duration_h=1, offset_minutes=0):
            """Return each requested interval's energy without reading a physical prediction."""
            return {
                row.start + timedelta(minutes=offset_minutes): (
                    0.8 * duration_h if 6 <= row.start.hour < 18 else 0.0
                )
                for row in conditions
                if row.received_at <= known_at
            }

    configuration = plant(count=2, shared=True, limit=500)
    forecast = learned_forecast(
        configuration,
        SITE,
        weather(NOW),
        NOW,
        {source_id: BoundaryModel() for source_id in learned_sources},
    )
    assert forecast.model == "solar-xgboost-production-v2"
    assert {row.source_id for row in forecast.energies} == set(learned_sources)
    noon = [row for row in forecast.energies if row.start == NOW + timedelta(hours=12)]
    assert len(noon) == len(learned_sources)
    assert sum(row.ac_kwh for row in noon) == pytest.approx(0.125)
    assert sum(row.dc_kwh for row in noon) == pytest.approx(len(learned_sources) * 0.2 / 0.96)
    for start in {row.start for row in forecast.energies}:
        rows = [row for row in forecast.energies if row.start == start]
        assert sum(row.ac_kwh for row in rows) <= 0.5 * rows[0].duration_h + 1e-12


def test_final_week_cannot_change_native_training_rows(monkeypatch):
    """Changing held-out outcomes never changes the rows or weights used to fit the native model."""
    from open_energy_planner.solar import boosting

    original_train, inputs = boosting._train, []

    def capture(rows):
        """Retain actual training input while executing the real native learner."""
        inputs.append(rows)
        return original_train(rows)

    monkeypatch.setattr(boosting, "_train", capture)
    monkeypatch.setattr(boosting, "_choose_trend", boosting._choose_trend.__wrapped__)
    normal, *_ = fit_case()
    reversed_week, *_ = fit_case(reverse_validation=True)
    assert inputs[0] == inputs[1]
    assert max(row[0] for row in inputs[0]) < START + timedelta(days=49)
    assert normal.summary.validation_hours == reversed_week.summary.validation_hours
    assert normal.summary.tree_mae_kwh != reversed_week.summary.tree_mae_kwh


def test_physical_prediction_changes_do_not_change_native_weights_or_predictions():
    """A changed physical baseline affects comparison scores but never the independent learning target."""
    configuration = plant()
    history, physical, weather, before = training_data()
    original = fit_boosted_model(
        configuration.sources[0],
        configuration.inverters[0],
        SITE,
        history,
        physical,
        weather,
        before,
    )
    low_physics = tuple(replace(row, dc_kwh=0.001, ac_kwh=0.001) for row in physical)
    changed = fit_boosted_model(
        configuration.sources[0],
        configuration.inverters[0],
        SITE,
        history,
        low_physics,
        weather,
        before,
    )
    assert original.model is not None and changed.model is not None
    assert original.model.model_json == changed.model.model_json
    assert original.summary.baseline_mae_kwh != changed.summary.baseline_mae_kwh
    future = tuple(
        SolarWeatherHour(before + timedelta(hours=hour), 600, 500, 100, 20, 2, before)
        for hour in (9, 14)
    )
    assert original.model.predict(future, SITE, known_at=before) == changed.model.predict(
        future,
        SITE,
        known_at=before,
    )


def test_valid_yield_above_the_old_factor_ceiling_remains_learnable():
    """A 2.304 kWh hour inside the declared 5 kW AC limit is not rejected for exceeding PVWatts."""
    result, _history, _physical, _weather, before = fit_case(constant=2.4)
    assert result.summary.excluded_counts.get("production_out_of_range", 0) == 0
    assert result.model is not None
    conditions = (SolarWeatherHour(before + timedelta(hours=14), 600, 500, 100, 20, 2, before),)
    prediction = result.model.predict(conditions, SITE, known_at=before)
    assert prediction[conditions[0].start] == pytest.approx(2.304, abs=0.1)


def test_missing_validation_day_is_distinguished_from_insufficient_training():
    """Enough older training cannot disguise a missing recent measurement day as a fitting shortage."""
    configuration = plant()
    history, physical, weather, before = training_data()
    cutoff = before - timedelta(days=1)
    history = replace(history, hours=tuple(row for row in history.hours if row.start < cutoff))
    result = fit_boosted_model(
        configuration.sources[0],
        configuration.inverters[0],
        SITE,
        history,
        physical,
        weather,
        before,
    )
    assert result.summary.training_hours >= 392
    assert result.summary.status == "insufficient_validation_history"
    assert result.summary.validation_days == 6


def test_a_dark_validation_day_counts_when_its_zero_measurements_are_complete():
    """A zero-yield day with reliable counters remains part of validation rather than disappearing."""
    configuration = plant()
    history, physical, weather, before = training_data()
    dark_start = before - timedelta(days=2)
    dark_end = dark_start + timedelta(days=1)
    total, hours = history.hours[0].total_kwh, [history.hours[0]]
    for previous, following in zip(history.hours, history.hours[1:]):
        increment = following.total_kwh - previous.total_kwh
        total += 0 if dark_start <= following.start < dark_end else increment
        hours.append(replace(following, total_kwh=total, meter_kwh=total))
    history = replace(history, hours=tuple(hours))
    weather = tuple(
        replace(row, ghi=0, dni=0, dhi=0) if dark_start <= row.start < dark_end else row
        for row in weather
    )
    physical = tuple(
        replace(row, ac_kwh=0, dc_kwh=0) if dark_start <= row.start < dark_end else row
        for row in physical
    )
    result = fit_boosted_model(
        configuration.sources[0],
        configuration.inverters[0],
        SITE,
        history,
        physical,
        weather,
        before,
    )
    assert result.summary.validation_days == 7
    assert result.summary.validation_hours >= 56
    assert result.summary.excluded_counts.get("low_production", 0) == 0
    assert result.summary.status != "insufficient_validation_history"


def test_meter_gap_and_negative_correction_remain_excluded_without_rewriting_history():
    """An absent counter hour and a decreasing correction are invalid evidence rather than dark-hour zeros."""
    configuration = plant()
    history, physical, weather, before = training_data()
    gap_at = START + timedelta(days=12, hours=12)
    correction_at = START + timedelta(days=16, hours=12)
    history = replace(
        history,
        hours=tuple(
            replace(row, total_kwh=row.total_kwh - 10) if row.start == correction_at else row
            for row in history.hours
            if row.start != gap_at
        ),
    )
    original = deepcopy(history)
    result = fit_boosted_model(
        configuration.sources[0],
        configuration.inverters[0],
        SITE,
        history,
        physical,
        weather,
        before,
    )
    assert history == original
    assert result.summary.excluded_counts["history_gap"] == 1
    assert result.summary.excluded_counts["meter_correction"] == 1
    assert result.summary.excluded_counts["production_out_of_range"] == 1
    assert result.model is not None


def test_training_weather_received_after_fit_time_does_not_enter_the_model():
    """A later weather receipt cannot make an otherwise valid measured hour known to an earlier fit."""
    configuration = plant()
    history, physical, weather, before = training_data()
    delayed = START + timedelta(days=12, hours=12)
    weather = tuple(
        replace(row, received_at=before + timedelta(minutes=1)) if row.start == delayed else row
        for row in weather
    )
    result = fit_boosted_model(
        configuration.sources[0],
        configuration.inverters[0],
        SITE,
        history,
        physical,
        weather,
        before,
    )
    assert result.summary.excluded_counts["weather_not_known"] == 1
    assert result.model is not None


def test_observed_unrestricted_history_admits_only_its_bounded_period():
    """Actual on/off feedback excludes affected hours without granting any hardware command."""
    from open_energy_planner.solar.boosting import unrestricted_interval

    configuration = plant()
    history, physical, weather, before = training_data()
    start = START + timedelta(days=3)
    source = replace(
        configuration.sources[0],
        can_curtail=True,
        curtailment_entity_id="binary_sensor.curtailed",
        unrestricted_history_start=start.isoformat(),
        unrestricted_history_end=before.isoformat(),
    )
    result = fit_boosted_model(
        source,
        configuration.inverters[0],
        SITE,
        history,
        physical,
        weather,
        before,
        feedback=CurtailmentHistory(
            "binary_sensor.curtailed",
            before + timedelta(hours=1),
            ((START, True), (start, False), (before, True)),
        ),
    )
    assert result.model is not None
    assert result.summary.excluded_counts["control_history_unconfirmed"] > 0
    assert result.summary.diagnostic_counts["possible_production_limitation"] > 0
    feedback = CurtailmentHistory(
        "binary_sensor.curtailed",
        before + timedelta(hours=1),
        ((START, True), (start, False), (before, True)),
    )
    assert unrestricted_interval(source, before - timedelta(hours=1), before, feedback)
    assert not unrestricted_interval(source, before, before + timedelta(hours=1), feedback)
    assert not unrestricted_interval(source, start - timedelta(hours=1), start, feedback)


def test_observed_curtailment_cannot_score_a_restricted_validation_day():
    """Feedback changing to curtailed before the last held-out day excludes that day rather than scoring zero."""
    configuration = plant()
    history, physical, weather, before = training_data()
    source = replace(
        configuration.sources[0],
        can_curtail=True,
        curtailment_entity_id="binary_sensor.curtailed",
        unrestricted_history_start=START.isoformat(),
        unrestricted_history_end=(before - timedelta(days=1)).isoformat(),
    )
    result = fit_boosted_model(
        source,
        configuration.inverters[0],
        SITE,
        history,
        physical,
        weather,
        before,
        feedback=CurtailmentHistory(
            "binary_sensor.curtailed", before, ((START, False), (before - timedelta(days=1), True))
        ),
    )
    assert result.model is None
    assert result.summary.training_days >= 28
    assert result.summary.validation_days == 6
    assert result.summary.status == "insufficient_validation_history"


def test_unrestricted_period_round_trip_retains_explicit_evidence_and_changes_identity():
    """An explicit dated declaration survives serialization and belongs to a distinct source configuration."""
    from open_energy_planner.solar.wire import configuration_document, parse_configuration

    configuration = plant()
    source = replace(
        configuration.sources[0],
        can_curtail=True,
        unrestricted_history_start=START.isoformat(),
        unrestricted_history_end=(START + timedelta(days=56)).isoformat(),
    )
    confirmed = replace(configuration, sources=(source,))
    restored = parse_configuration(configuration_document(confirmed))
    assert restored == confirmed
    assert restored.version == confirmed.version
    assert restored.version != configuration.version
    assert restored.sources[0].unrestricted_history_start == "2026-04-01T00:00:00+00:00"
    assert restored.sources[0].unrestricted_history_end == "2026-05-27T00:00:00+00:00"


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-04-01T00:00:00+00:00", None),
        (None, "2026-05-27T00:00:00+00:00"),
        ("invalid-date", "2026-05-27T00:00:00+00:00"),
        ("2026-05-27T00:00:00+00:00", "2026-04-01T00:00:00+00:00"),
        ("2026-04-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00"),
    ],
)
def test_incomplete_or_reversed_unrestricted_period_is_rejected(start, end):
    """Unknown, malformed or nonpositive confirmation boundaries cannot admit controllable history."""
    with pytest.raises(ValueError):
        replace(
            plant().sources[0],
            unrestricted_history_start=start,
            unrestricted_history_end=end,
        )


def test_confirmation_extension_preserves_forward_family_but_geometry_and_algorithm_do_not():
    """Extending historical evidence reuses original captures while physical or algorithm changes cannot."""
    from open_energy_planner.solar.source_selection import family

    configuration = plant()
    source = replace(
        configuration.sources[0],
        can_curtail=True,
        unrestricted_history_start=START.isoformat(),
        unrestricted_history_end=(START + timedelta(days=30)).isoformat(),
    )
    confirmed = replace(configuration, sources=(source,))
    extended_source = replace(
        source, unrestricted_history_end=(START + timedelta(days=35)).isoformat()
    )
    extended = replace(confirmed, sources=(extended_source,))
    original_family = family(confirmed, source, SITE, learned_policy="a" * 64)
    assert confirmed.version != extended.version
    assert family(extended, extended_source, SITE, learned_policy="a" * 64) == original_family
    moved_source = replace(extended_source, tilt=45)
    moved = replace(extended, sources=(moved_source,))
    assert family(moved, moved_source, SITE, learned_policy="a" * 64) != original_family
    assert family(extended, extended_source, SITE, learned_policy="b" * 64) != original_family


def test_confirming_past_control_evidence_scores_original_forward_captures_without_replacing_them(
    tmp_path,
):
    """Saved forecasts wait for the linked feedback and become comparable without rewriting captured predictions."""
    from types import SimpleNamespace

    from open_energy_planner.solar.calculation.model import SolarForecast
    from open_energy_planner.solar.source_selection import choose_sources
    from open_energy_planner.solar.storage import SolarStore
    from tests.database.support.archive import make_store

    configuration = plant()
    configuration = replace(
        configuration,
        sources=(
            replace(
                configuration.sources[0],
                can_curtail=True,
                curtailment_entity_id="binary_sensor.curtailed",
            ),
        ),
    )
    store = make_store(tmp_path, SolarStore)

    def forecast(now, amount, *, model):
        """Declare two future days of fixed hourly production without using any measured outcomes."""
        return SolarForecast(
            configuration.version,
            SITE,
            now,
            "weather",
            tuple(
                SolarEnergy("pv0", now + timedelta(hours=index), 1, amount / 0.96, amount)
                for index in range(48)
            ),
            model=model,
        )

    fitted = (SimpleNamespace(source_id="pv0"),)
    for day_index in range(3):
        instant = START + timedelta(days=day_index)
        choose_sources(
            store,
            configuration,
            forecast(instant, 0.5, model="pvwatts-v2"),
            forecast(instant, 0.4, model="solar-xgboost-production-v2"),
            fitted,
            {},
            {},
            instant,
            ml_policies={"pv0": "a" * 64},
        )
    captures = list(store.documents.iter_prefix("source-day-"))
    assert len(captures) == 3
    now = START + timedelta(days=4, hours=1)
    history = EnergyHistory(
        "sensor.pv0",
        tuple(
            EnergyHour(START + timedelta(hours=index), index * 0.4, index * 0.4)
            for index in range(97)
        ),
    )
    baseline = forecast(now, 0.5, model="pvwatts-v2")
    candidate = forecast(now, 0.4, model="solar-xgboost-production-v2")
    selected, status = choose_sources(
        store,
        configuration,
        baseline,
        candidate,
        fitted,
        {},
        {"sensor.pv0": history},
        now,
        ml_policies={"pv0": "a" * 64},
    )
    assert selected["pv0"][0] == "pvwatts"
    assert status["pv0"]["complete_days"] == 0
    captures = list(store.documents.iter_prefix("source-day-"))
    confirmed_source = replace(
        configuration.sources[0],
        unrestricted_history_start=START.isoformat(),
        unrestricted_history_end=now.isoformat(),
    )
    confirmed = replace(configuration, sources=(confirmed_source,))
    selected, status = choose_sources(
        store,
        confirmed,
        baseline,
        candidate,
        fitted,
        {},
        {"sensor.pv0": history},
        now,
        ml_policies={"pv0": "a" * 64},
        feedback={
            "binary_sensor.curtailed": CurtailmentHistory(
                "binary_sensor.curtailed", now, ((START, False),)
            )
        },
    )
    assert selected["pv0"][0] == "ml"
    assert status["pv0"]["complete_days"] == 3
    assert status["pv0"]["mae_kwh"]["ml"] == pytest.approx(0, abs=1e-12)
    assert list(store.documents.iter_prefix("source-day-")) == captures


def test_optional_inference_failure_preserves_the_physical_forecast(tmp_path, monkeypatch, caplog):
    """A native inference fault retains the baseline and never exposes its private error value."""
    from open_energy_planner.solar import runtime as runtime_module

    runtime, now = runtime_case(tmp_path)

    def unavailable(*args, **kwargs):
        """Represent an inference-only fault after a usable historical model was fitted."""
        raise RuntimeError("private-inference-source-and-token")

    monkeypatch.setattr(runtime_module, "learned_forecast", unavailable)
    with caplog.at_level("DEBUG", logger="open_energy_planner.solar.runtime"):
        assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.forecast.model == "pvwatts-v2"
    assert runtime.candidate is None
    assert runtime.document(now)["learning"]["pv0"]["status"] == "training_failed"
    assert "private-inference-source-and-token" not in caplog.text
    assert "RuntimeError" in caplog.text


def multiyear_geometry_case():
    """Retain 44,800 measured intervals while only the latest eight weeks have downloaded forecast weather."""
    _history, physical, weather, before = training_data()
    first = before - timedelta(hours=44801)
    total, readings = 100.0, []
    for index in range(44801):
        instant = first + timedelta(hours=index)
        total += 0.24 if 8 <= instant.hour < 11 else 0.864 if 11 <= instant.hour < 16 else 0
        readings.append(EnergyHour(instant, total, total % 20))
    return EnergyHistory("sensor.pv0", tuple(readings)), physical, weather, before


def test_repeated_multiyear_fits_reuse_history_geometry_without_prediction_eviction(monkeypatch):
    """Three real native fits share 179,200 history midpoint calculations while independent prediction queries keep their own geometry."""
    from pvlib import solarposition

    configuration = plant()
    history, physical, weather, before = multiyear_geometry_case()
    get_solarposition = solarposition.get_solarposition
    computed = []

    def calculate(times, latitude, longitude, **options):
        """Count actual pvlib sample work without replacing astronomy or the native learner."""
        computed.append((len(times), latitude, longitude))
        return get_solarposition(times, latitude, longitude, **options)

    monkeypatch.setattr(solarposition, "get_solarposition", calculate)
    arguments = (
        configuration.sources[0],
        configuration.inverters[0],
        SITE,
        history,
        physical,
        weather,
    )
    first = fit_boosted_model(*arguments, before)
    repeated = fit_boosted_model(*arguments, before)
    earlier = fit_boosted_model(*arguments, before - timedelta(days=7), known_at=before)
    assert first == repeated
    assert first.model is not None and earlier.challenger is not None
    assert earlier.challenger.trained_until < first.challenger.trained_until
    assert len(computed) == 1
    assert computed == [(179200, 52, 6)]
    for day in range(12):
        instant = before + timedelta(days=day, hours=12)
        conditions = (SolarWeatherHour(instant, 600, 500, 100, 20, 2, before),)
        assert first.model.predict(
            conditions, SITE, known_at=before, duration_h=0.25, offset_minutes=15
        )
    assert fit_boosted_model(*arguments, before) == first
    assert len(computed) == 13
    assert [count for count, _latitude, _longitude in computed].count(179200) == 1


def test_history_geometry_cache_keeps_exact_outputs_and_separates_all_query_inputs(monkeypatch):
    """Location, timestamp sequence and duration changes recompute geometry while retained tuples cannot be mutated."""
    from pvlib import solarposition

    from open_energy_planner.solar import boosting

    instants = (
        datetime(2024, 2, 29, 6, tzinfo=UTC),
        datetime(2024, 6, 21, 3, tzinfo=UTC),
        datetime(2024, 12, 21, 16, tzinfo=UTC),
    )
    queries = (
        (instants, SITE, 1),
        (instants, replace(SITE, latitude=53), 1),
        (instants, replace(SITE, longitude=7), 1),
        (instants, replace(SITE, timezone="UTC"), 1),
        (instants[:-1], SITE, 1),
        (instants, SITE, 0.25),
    )
    expected = tuple(boosting.solar_geometry(*query) for query in queries)
    get_solarposition = solarposition.get_solarposition
    computed = []

    def calculate(times, latitude, longitude, **options):
        """Execute the original pvlib query and record each independent cache miss."""
        computed.append((len(times), latitude, longitude))
        return get_solarposition(times, latitude, longitude, **options)

    monkeypatch.setattr(solarposition, "get_solarposition", calculate)
    boosting._history_geometry.cache_clear()
    for query, original in zip(queries, expected, strict=True):
        result = boosting._history_geometry(*query)
        assert result == original
        assert boosting._history_geometry(*query) is result
        with pytest.raises(TypeError):
            result[instants[0]] = (0, 0, 0, 0)
        with pytest.raises(TypeError):
            result[instants[0]][0] = 0
        assert boosting._history_geometry(*query) == original
    assert len(computed) == 6
    assert [count for count, _latitude, _longitude in computed] == [12, 12, 12, 12, 8, 3]


def test_history_geometry_cache_evicts_old_queries_instead_of_growing_without_bound(monkeypatch):
    """Nine independent history cohorts exceed the eight-entry budget and revisiting the first computes again."""
    from pvlib import solarposition

    from open_energy_planner.solar import boosting

    get_solarposition = solarposition.get_solarposition
    computed = []

    def calculate(times, latitude, longitude, **options):
        """Retain real astronomical outputs while counting bounded-cache eviction behavior."""
        computed.append(len(times))
        return get_solarposition(times, latitude, longitude, **options)

    monkeypatch.setattr(solarposition, "get_solarposition", calculate)
    boosting._history_geometry.cache_clear()
    queries = tuple((START + timedelta(days=day, hours=12),) for day in range(9))
    original = boosting._history_geometry(queries[0], SITE)
    for query in queries[1:]:
        boosting._history_geometry(query, SITE)
    assert boosting._history_geometry.cache_info().currsize == 8
    assert boosting._history_geometry(queries[0], SITE) == original
    assert computed == [4] * 10
