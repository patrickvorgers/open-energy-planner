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
from open_energy_planner.solar.wire import document
from tests.app.solar.test_solar import SITE, plant

START = datetime(2026, 4, 1, tzinfo=UTC)


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


def test_nonlinear_shadow_pattern_beats_a_single_factor_and_replays_json():
    """Unseen days retain the 25% morning and 90% afternoon response that one scalar cannot express."""
    result, _history, _physical, _weather, before = fit_case()
    assert result.model is not None
    assert result.summary.status == "nonlinear_shadow"
    assert result.summary.validation_hours == 56
    assert result.summary.tree_mae_kwh < result.summary.linear_mae_kwh * 0.2
    physical = tuple(
        SolarEnergy("pv0", before + timedelta(hours=hour), 1, 1, 0.96) for hour in (9, 14)
    )
    weather = tuple(SolarWeatherHour(row.start, 600, 500, 100, 20, 2, before) for row in physical)
    restored = SolarBoostedModel.restore(document(result.model))
    predicted = restored.factors(physical, weather, known_at=before)
    assert predicted[physical[0].start] == pytest.approx(0.25, abs=0.05)
    assert predicted[physical[1].start] == pytest.approx(0.9, abs=0.05)
    assert predicted == result.model.factors(physical, weather, known_at=before)


def test_cleaning_retains_repeated_shade_and_original_counter_payloads():
    """One impossible jump and one isolated zero are excluded, while recurring morning shade is retained."""
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
    assert result.summary.excluded_counts["statistical_outlier"] == 1
    assert result.summary.training_hours == 390
    assert result.summary.validation_hours == 56


def test_constant_correction_does_not_promote_complexity_without_material_gain():
    """A correct 80% scalar leaves no material held-out error for a tree model to improve."""
    result, *_ = fit_case(constant=0.8)
    assert result.model is None
    assert result.summary.status == "linear_preferred"
    assert result.summary.linear_mae_kwh < 0.005


def test_validation_outcomes_are_not_trimmed_to_make_trees_win():
    """A changed final week is scored as observed rather than excluded using training outlier fences."""
    result, *_ = fit_case(reverse_validation=True)
    assert result.model is None
    assert result.summary.validation_hours == 56
    assert result.summary.excluded_counts.get("statistical_outlier", 0) == 0


def test_model_and_weather_cannot_be_backdated_before_they_were_known():
    """Historical target cutoff cannot disguise a later model choice or a later weather receipt."""
    result, _history, _physical, _weather, before = fit_case()
    physical = (SolarEnergy("pv0", before + timedelta(hours=9), 1, 1, 0.96),)
    weather = (SolarWeatherHour(physical[0].start, 600, 500, 100, 20, 2, before),)
    with pytest.raises(ValueError, match="created after prediction"):
        result.model.factors(physical, weather, known_at=before - timedelta(seconds=1))
    future_receipt = (replace(weather[0], received_at=before + timedelta(seconds=1)),)
    assert result.model.factors(physical, future_receipt, known_at=before) == {}


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
        replace(configuration.sources[0], can_curtail=curtail, measurement_boundary=boundary),
        replace(configuration.inverters[0], hybrid=hybrid),
        SITE,
        history,
        physical,
        weather,
        before,
    )
    assert result.model is None
    assert result.summary.status == "blocked"
    assert result.summary.issue == (
        "curtailment_feedback_required" if curtail else "hybrid_route_history_required"
    )


def runtime_case(tmp_path, *, store=None):
    """Build real geometry for two roofs, with repeated morning shade measured only on the first roof."""
    from open_energy_planner.solar.calculation.predictor import PVWattsPredictor
    from open_energy_planner.solar.runtime import SolarRuntime
    from open_energy_planner.solar.storage import SolarStore
    from tests.app.solar.test_solar import START as NOW
    from tests.app.solar.test_solar import weather
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
    assert runtime.candidate.model == "solar-learned-v1"
    assert runtime.forecast.model == "pvwatts-v2"
    assert runtime.source_selection["pv0"]["active"] == "pvwatts"
    models = runtime.store.documents.read(
        "quality-source-models-" + runtime.candidate.version + ".json"
    )
    assert models == {"pv0": "solar-xgboost-v1", "pv1": "pvwatts-v2"}
    training = runtime.store.documents.read("ml-training-" + result.archive_reference + ".json")
    assert training["summary"]["validation_hours"] >= 28
    assert training["history_version"] == runtime.calibrations[0].history_version
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
    assert public["candidate_model"] == "solar-xgboost-v1"
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
    assert runtime.candidate.model == "pvwatts-calibrated-v2"
    assert runtime.document(now)["learning"]["pv0"]["status"] == "training_failed"
    assert "private-solar-location-and-provider-token" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_hourly_tree_correction_preserves_quarter_geometry_and_shared_inverter_cap():
    """Per-hour corrections scale DC before two roofs share one 0.5 kW AC limit, retaining changing quarter geometry."""
    from open_energy_planner.solar.calculation.predictor import PVWattsPredictor
    from tests.app.solar.test_solar import START as NOW

    configuration = plant(count=2, shared=True, limit=500, tilt=30, orientation="east")
    instant = NOW + timedelta(hours=8)
    conditions = (SolarWeatherHour(instant, 800, 700, 100, 20, 2, NOW),)
    predictor = PVWattsPredictor()
    baseline = predictor.quarters(configuration, SITE, conditions)
    corrected = predictor.quarters(
        configuration,
        SITE,
        conditions,
        hourly_factors={("pv0", instant): 0.8, ("pv1", instant): 1.5},
    )
    for source_id, factor in (("pv0", 0.8), ("pv1", 1.5)):
        actual = [row for row in corrected if row.source_id == source_id]
        original = [row for row in baseline if row.source_id == source_id]
        assert len({round(row.dc_kwh, 5) for row in actual}) > 1
        assert [row.dc_kwh for row in actual] == pytest.approx(
            [row.dc_kwh * factor for row in original]
        )
    for quarter in range(4):
        start = instant + timedelta(minutes=quarter * 15)
        assert sum(row.ac_kwh for row in corrected if row.start == start) <= 0.125 + 1e-12


def test_final_week_cannot_change_training_rows_or_the_scalar_comparison(monkeypatch):
    """Changing every held-out outcome leaves native training and the separately trained scalar unchanged."""
    from open_energy_planner.solar import boosting

    original_train, inputs = boosting._train, []

    def capture(rows):
        """Retain actual training input while executing the real native learner."""
        inputs.append(rows)
        return original_train(rows)

    monkeypatch.setattr(boosting, "_train", capture)
    normal, *_ = fit_case()
    reversed_week, *_ = fit_case(reverse_validation=True)
    assert inputs[0] == inputs[1]
    assert max(row[0] for row in inputs[0]) < START + timedelta(days=49)
    # 49 training days each contain three 0.25-ratio and five 0.9-ratio hours.
    scalar = (49 * (3 * 0.25 + 5 * 0.9) * 0.96**2 + 1) / (49 * 8 * 0.96**2 + 1)
    assert reversed_week.summary.linear_mae_kwh == pytest.approx(0.96 * scalar)
    assert normal.model is not None and reversed_week.model is None
