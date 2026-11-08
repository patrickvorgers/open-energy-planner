"""Measure multi-season learning and recency influence without allowing holdout leakage."""

from datetime import UTC, datetime, timedelta
from math import cos, pi, sin

import numpy as np
import pytest

from open_energy_planner.solar.boosting import (
    FEATURE_NAMES,
    _booster,
    _train,
)


def sample(instant, target):
    """Specify cyclic weather features with no absolute-year identifier or model-derived target."""
    phase = (instant.timetuple().tm_yday - 1) / 365 * 2 * pi
    features = (0, -1, sin(phase), cos(phase), 0.6, 0.5, 0.1, 0.5, 0.2, 0.5, 0, -1, 1)
    return instant, None, target, features


def predict_rows(model_json, rows):
    """Score independent held-out targets through the pinned native learner."""
    from xgboost import DMatrix

    return _booster(model_json).predict(
        DMatrix(np.array([row[3] for row in rows]), feature_names=list(FEATURE_NAMES), nthread=1)
    )


def test_training_age_does_not_change_equal_condition_influence():
    """Repeating older measurements has the same influence as repeating recent measurements."""
    start = datetime(2020, 1, 1, tzinfo=UTC)
    features = sample(start, 0.5)[3]
    rows = (
        tuple(
            (start + timedelta(days=index * 365), None, target, features)
            for index, target in enumerate((0.2, 0.2, 0.8, 0.8))
        )
        * 20
    )
    reversed_yield = tuple((row[0], None, 1 - row[2], row[3]) for row in rows)
    assert predict_rows(_train(rows), rows) == pytest.approx([0.5] * len(rows), abs=0.001)
    assert predict_rows(_train(reversed_yield), rows) == pytest.approx([0.5] * len(rows), abs=0.001)


def test_multiple_forecast_realizations_share_one_measurements_training_weight():
    """Ten forecasts paired to one low reading cannot outweigh one high reading at another hour."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    features = sample(start, 0.5)[3]
    rows = ((start, None, 0.2, features),) * 10 + (
        (start + timedelta(hours=1), None, 0.8, features),
    )
    assert predict_rows(_train(rows), rows) == pytest.approx([0.5] * 11, abs=0.001)


def test_multiyear_history_recovers_a_season_missing_from_the_latest_year():
    """Earlier winters teach low winter yield that a latest-year summer-only sample cannot reveal."""
    start = datetime(2022, 1, 1, 12, tzinfo=UTC)
    cutoff = datetime(2025, 1, 1, tzinfo=UTC)
    all_rows = []
    for index in range((cutoff - start).days):
        instant = start + timedelta(days=index)
        if instant.year == 2024 and instant.month not in (5, 6, 7, 8, 9, 10):
            continue
        all_rows.append(sample(instant, 0.25 if instant.month in (11, 12, 1, 2) else 0.85))
    latest = tuple(row for row in all_rows if row[0].year == 2024)
    validation = tuple(sample(cutoff + timedelta(days=index, hours=12), 0.25) for index in range(7))
    one_year = predict_rows(_train(latest), validation)
    multiyear = predict_rows(_train(tuple(all_rows)), validation)
    one_year_error = float(np.abs(one_year - 0.25).mean())
    multiyear_error = float(np.abs(multiyear - 0.25).mean())
    assert one_year_error > 0.5
    assert multiyear_error < 0.05
    assert multiyear_error < one_year_error
    assert max(row[0] for row in all_rows) < min(row[0] for row in validation)
