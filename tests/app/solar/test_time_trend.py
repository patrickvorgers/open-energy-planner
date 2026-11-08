"""Use independent multi-year outcomes to distinguish persistent change from a bad year."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import cos, exp, pi, sin

import numpy as np
import pytest

from open_energy_planner.solar.boosting import _choose_trend, _predict_rows, _train
from open_energy_planner.solar.trend import TrendRateOutOfRange, YieldTrend, estimate_trend


def production_rows(*, rate=0, bad_year=None, dark_year=None):
    """Generate five seasons repeated over years with separately known weather and yield drift."""
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2025, 1, 1, tzinfo=UTC)
    rows = []
    for day in range((end - start).days):
        for hour in (10, 13):
            instant = start + timedelta(days=day, hours=hour)
            phase = (instant.timetuple().tm_yday - 1) / 365.2425 * 2 * pi
            sunshine = 0.15 if instant.year == dark_year else 0.7
            features = (
                sin(hour / 24 * 2 * pi),
                cos(hour / 24 * 2 * pi),
                sin(phase),
                cos(phase),
                sunshine,
                sunshine * 0.8,
                sunshine * 0.2,
                0.5,
                0.2,
                0.5,
                0,
                -1,
                1,
            )
            years = (instant - start).total_seconds() / 86400 / 365.2425
            actual = (1 + 0.3 * sin(phase)) * sunshine * exp(rate * years)
            if instant.year == bad_year:
                actual *= 0.8
            rows.append((instant, None, actual, features))
    return tuple(rows)


@pytest.mark.parametrize("rate", (-0.02, -0.005, 0.01))
def test_persistent_yield_change_is_selected_and_improves_unseen_future(rate):
    """Persistent small declines and increases improve past seasonal years and an unseen sixth year."""
    rows = production_rows(rate=rate)
    model_json, trend, assessment = _choose_trend(rows, 4.0)
    assert trend is not None
    assert assessment.status == "active"
    assert trend.annual_change == pytest.approx(exp(rate) - 1, abs=0.003)
    assert len(assessment.folds) == 8
    assert all(
        fold.training_end <= fold.validation_start < fold.validation_end
        for fold in assessment.folds
    )
    # The independent future target repeats an earlier year's weather, not its yield.
    future = tuple(
        (row[0].replace(year=2025), None, row[2] * exp(rate * 5), row[3])
        for row in rows[:720]
        if (row[0].month, row[0].day) != (2, 29)
    )
    plain = _predict_rows(_train(rows), future, 4.0)
    adjusted = _predict_rows(model_json, future, 4.0, trend)
    truth = np.array([row[2] for row in future])
    assert np.abs(np.array(adjusted) - truth).mean() < np.abs(np.array(plain) - truth).mean() * 0.3


@pytest.mark.parametrize("bad_year", (2021, 2023, 2024))
def test_one_bad_year_does_not_activate_a_permanent_decline(bad_year):
    """An isolated low-yield year before or during the seasonal tests cannot establish a trend."""
    rows = production_rows(bad_year=bad_year)
    model_json, trend, assessment = _choose_trend(rows, 4.0)
    assert trend is None and assessment.status != "active"
    assert model_json == _train(rows)


def test_dark_weather_year_does_not_become_degradation():
    """Weather features explain a dark year without extrapolating a permanent yield loss."""
    rows = production_rows(dark_year=2024)
    _model_json, trend, assessment = _choose_trend(rows, 4.0)
    assert trend is None and assessment.status != "active"


def test_missing_quarter_keeps_equal_weight_ml_instead_of_shortening_validation():
    """A missing season prevents trend admission even when the remaining decline is consistent."""
    rows = tuple(
        row
        for row in production_rows(rate=-0.02)
        if not (row[0].year == 2023 and row[0].month in (4, 5, 6))
    )
    model_json, trend, assessment = _choose_trend(rows, 4.0)
    assert trend is None and assessment.status == "insufficient_history"
    assert model_json == _train(rows)


def test_short_history_does_not_disable_the_ordinary_learner():
    """Two years alone cannot prove a persistent trend but still fit equal-weight ML."""
    rows = tuple(row for row in production_rows(rate=-0.02) if row[0].year >= 2023)
    model_json, trend, assessment = _choose_trend(rows, 4.0)
    assert trend is None and assessment.status == "insufficient_history"
    assert _predict_rows(model_json, rows[-20:], 4.0)


def test_trend_estimation_retains_zero_production_and_rejects_unsafe_rates():
    """Zeros remain in monthly targets and excessive measured shifts are rejected, never clipped."""
    rows = production_rows(rate=-0.02)
    baseline = [
        row[2] / exp(-0.02 * (row[0] - rows[0][0]).total_seconds() / 86400 / 365.2425)
        for row in rows
    ]
    estimate = estimate_trend(rows, baseline)
    assert estimate.annual_change == pytest.approx(-0.0198, abs=0.001)
    with pytest.raises(TrendRateOutOfRange):
        estimate_trend(production_rows(rate=-0.2), baseline)
    zero_rows = tuple((row[0], None, 0.0, row[3]) for row in rows)
    assert estimate_trend(zero_rows, baseline) is None


def test_unsafe_measured_change_reports_instability_instead_of_missing_history():
    """Many years with an excessive recurring decline retain ML and explain the actual safety rejection."""
    rows = production_rows(rate=-0.2)
    model_json, trend, report = _choose_trend(rows, 4.0)
    assert trend is None and report.status == "unstable"
    assert "safety range" in report.reason and "Learning data from" in report.reason
    assert model_json == _train(rows)


@pytest.mark.parametrize("rate", (float("nan"), float("inf"), True, -1, 1))
def test_archived_trend_rejects_nonfinite_or_unbounded_rates(rate):
    """Invalid archived rates never reach exponential prediction arithmetic."""
    with pytest.raises(ValueError, match="Invalid solar yield trend"):
        YieldTrend(rate, datetime(2025, 1, 1, tzinfo=UTC))


def test_trend_factor_uses_elapsed_time_instead_of_calendar_year_buckets():
    """A continuous one-percent annual decrease scales the reference without midnight jumps."""
    reference = datetime(2024, 12, 31, 23, tzinfo=UTC)
    trend = YieldTrend(-0.01, reference)
    assert trend.factor(reference) == 1
    assert trend.factor(reference + timedelta(days=365.2425)) == pytest.approx(exp(-0.01))
    assert replace(trend, reference=reference + timedelta(hours=1)).factor(reference) > 1


def test_seasonal_backtests_never_train_on_the_outcomes_they_score(monkeypatch):
    """Every real native fold predicts strictly later observations or its own estimation rows."""
    from open_energy_planner.solar import boosting

    fitted_rows = {}
    comparisons = []
    original_train, original_predict = boosting._train, boosting._predict_rows

    def capture_train(rows):
        """Track the actual observations supplied to the native learner by archive digest."""
        model_json = original_train(rows)
        fitted_rows[model_json] = tuple(row[0] for row in rows)
        return model_json

    def capture_predict(model_json, rows, maximum, trend=None):
        """Record true fit and prediction timestamps while executing native inference."""
        fitted = fitted_rows[model_json]
        scored = tuple(row[0] for row in rows)
        if scored != fitted:
            comparisons.append((max(fitted), min(scored)))
        return original_predict(model_json, rows, maximum, trend)

    monkeypatch.setattr(boosting, "_train", capture_train)
    monkeypatch.setattr(boosting, "_predict_rows", capture_predict)
    _model_json, trend, report = boosting._choose_trend.__wrapped__(
        production_rows(rate=-0.02), 4.0
    )
    assert trend is not None and report.status == "active"
    assert len(comparisons) == 16
    assert all(trained < predicted for trained, predicted in comparisons)


def test_production_fit_retains_the_selected_trend_and_does_not_tune_on_its_final_week():
    """The real cumulative-history fit carries its trend into archives without using final-week outcomes."""
    from open_energy_planner.history.model import EnergyHistory, EnergyHour
    from open_energy_planner.solar.boosting import SolarBoostedModel, fit_boosted_model
    from open_energy_planner.solar.calculation.model import SolarEnergy, SolarWeatherHour
    from open_energy_planner.solar.wire import document
    from tests.app.solar.test_solar import SITE, plant

    start = datetime(2020, 1, 1, tzinfo=UTC)
    before = datetime(2025, 1, 15, tzinfo=UTC)
    configuration = plant()
    weather, physical, observations = [], [], []
    for hour in range(-1, int((before - start) / timedelta(hours=1))):
        instant = start + timedelta(hours=hour)
        lit = 8 <= instant.hour < 16
        years = (instant - start).total_seconds() / 86400 / 365.2425
        observations.append((instant, 0.7 * exp(-0.02 * years) if lit else 0))
        weather.append(SolarWeatherHour(instant, 600 * lit, 500 * lit, 100 * lit, 20, 2, before))
        physical.append(SolarEnergy("pv0", instant, 1, float(lit), 0.96 * lit))

    def fit(final_week_multiplier):
        """Vary only the outer held-out outcomes while retaining every earlier meter increment."""
        total, readings = 100.0, []
        for instant, actual in observations:
            total += actual * (
                final_week_multiplier if instant >= before - timedelta(days=7) else 1
            )
            readings.append(EnergyHour(instant, total, total))
        return fit_boosted_model(
            configuration.sources[0],
            configuration.inverters[0],
            SITE,
            EnergyHistory("sensor.pv0", tuple(readings)),
            tuple(physical),
            tuple(weather),
            before,
        )

    result = fit(1)
    changed = fit(0.2)
    assert result.model is not None and result.model.time_trend is not None
    assert result.summary.time_trend.status == "active"
    assert changed.summary.time_trend == result.summary.time_trend
    assert result.model.time_trend.annual_change == pytest.approx(-0.0198, abs=0.003)
    restored = SolarBoostedModel.restore(document(result.model))
    assert restored == result.model
    future = (replace(weather[-12], start=before + timedelta(hours=12)),)
    assert restored.predict(future, SITE, known_at=before) == result.model.predict(
        future, SITE, known_at=before
    )
