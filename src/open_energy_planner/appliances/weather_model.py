"""Bounded CPU tree regression for weather-sensitive consumer energy, with plain JSON archives."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from itertools import pairwise
from math import isfinite
from statistics import mean
from zoneinfo import ZoneInfo

import numpy as np

from open_energy_planner.core.units import DAYS_PER_WEEK, HOURS_PER_DAY
from open_energy_planner.demand.calculation.forecasting import (
    DAILY_HARMONICS,
    MIN_CONTIGUOUS_RIDGE_DAYS,
    MIN_RIDGE_TRAINING_DAYS,
    RECENT_HISTORY_HOURS,
    features,
)
from open_energy_planner.demand.calculation.model import digest, quantity
from open_energy_planner.prices.model import aware

TREE_ROUNDS = 96
TREE_DEPTH = 3
TREE_LEARNING_RATE = 0.08
TREE_MIN_CHILD_WEIGHT = 12
MAX_MODEL_JSON_BYTES = 2 * 1024 * 1024
MODEL_SCHEMA = "appliance-weather-xgboost-v1"
FEATURE_NAMES = (
    "intercept",
    *(
        name
        for harmonic in DAILY_HARMONICS
        for name in (f"hour_sin_{harmonic}", f"hour_cos_{harmonic}")
    ),
    *(f"weekday_{day}" for day in range(DAYS_PER_WEEK - 1)),
    "year_sin",
    "year_cos",
    "previous_24h_mean_kwh",
    "temperature_scaled",
    "heating_degrees_scaled",
    "cooling_degrees_scaled",
)


class TrainingExclusionReason(StrEnum):
    """Explain unavailable inputs without classifying valid high consumption as an outlier."""

    PREVIOUS_DAY_INCOMPLETE = "previous_day_incomplete"
    TEMPERATURE_UNAVAILABLE = "temperature_unavailable"


@lru_cache(maxsize=16)
def _booster(model_json, model_digest):
    """Keep native objects private and validate bounded non-executable data before native loading."""
    from xgboost import Booster
    from xgboost.core import XGBoostError

    if (
        not isinstance(model_json, str)
        or len(model_json.encode()) > MAX_MODEL_JSON_BYTES
        or digest(model_json) != model_digest
    ):
        raise ValueError("Invalid weather model archive")
    try:
        document = json.loads(model_json)
        learner = document["learner"]
        trees = learner["gradient_booster"]["model"]["trees"]
        if (
            learner["feature_names"] != list(FEATURE_NAMES)
            or learner["gradient_booster"]["name"] != "gbtree"
            or learner["objective"]["name"] != "reg:squarederror"
            or int(learner["learner_model_param"]["num_feature"]) != len(FEATURE_NAMES)
            or not 1 <= len(trees) <= TREE_ROUNDS
            or any(
                not 1 <= int(tree["tree_param"]["num_nodes"]) <= 2 ** (TREE_DEPTH + 1) - 1
                for tree in trees
            )
        ):
            raise ValueError("Unsupported weather model structure")
        booster = Booster(params={"nthread": 1, "device": "cpu"})
        booster.load_model(bytearray(model_json.encode()))
    except (KeyError, TypeError, OverflowError, RecursionError, XGBoostError) as error:
        raise ValueError("Invalid weather model archive") from error
    return booster


@dataclass(frozen=True)
class WeatherDemandModel:
    """Store only scalar metadata and native JSON. Predictions cannot extend observed weather support."""

    timezone: str
    configuration_version: str
    training_version: str
    trained_until: datetime
    recent_kwh: float
    minimum_temperature: float
    maximum_temperature: float
    model_json: str
    model_digest: str
    source_hours: int
    training_hours: int
    excluded_periods: tuple[tuple[str, str, str], ...]
    feature_names: tuple[str, ...] = FEATURE_NAMES
    schema: str = MODEL_SCHEMA
    model_id = "xgboost-weather-v1"
    residual_profile = True
    temperature_enabled = True

    @classmethod
    def fit(cls, configuration, samples, *, temperatures=(), known_at=None):
        """Fit complete preceding-day rows without outlier trimming or post-training weather knowledge.

        The caller supplies completed device measurements only. Recent load at
        prediction stays fixed at the training cutoff, never a future realization.
        Missing hours and temperatures are omitted, never filled with zero.
        """
        from xgboost import DMatrix, train

        if len(samples) < MIN_RIDGE_TRAINING_DAYS * HOURS_PER_DAY:
            raise ValueError("Insufficient weather model history")
        if any(first.start >= second.start for first, second in pairwise(samples)):
            raise ValueError("Weather model history must be ordered and unique")
        cutoff = samples[-1].start + timedelta(hours=1)
        weather = {
            item.start: item.celsius
            for item in temperatures
            if item.known_at <= (known_at or cutoff)
        }
        measurements = {item.start: item.residual_kwh for item in samples}
        rows, targets, used_temperatures, excluded_periods = [], [], [], []
        for sample in samples:
            prior = [
                measurements.get(sample.start - timedelta(hours=offset))
                for offset in range(1, RECENT_HISTORY_HOURS + 1)
            ]
            if sample.start not in weather or any(value is None for value in prior):
                reason = (
                    TrainingExclusionReason.TEMPERATURE_UNAVAILABLE
                    if sample.start not in weather
                    else TrainingExclusionReason.PREVIOUS_DAY_INCOMPLETE
                )
                start, end = (
                    sample.start.isoformat(),
                    (sample.start + timedelta(hours=1)).isoformat(),
                )
                if excluded_periods and excluded_periods[-1][1:] == (start, reason):
                    excluded_periods[-1] = (excluded_periods[-1][0], end, reason)
                else:
                    excluded_periods.append((start, end, reason))
                continue
            rows.append(
                features(
                    sample.start,
                    configuration.timezone,
                    temperature=weather[sample.start],
                    recent=mean(prior),
                )
            )
            targets.append(sample.residual_kwh)
            used_temperatures.append(weather[sample.start])
        if len(rows) < MIN_CONTIGUOUS_RIDGE_DAYS * HOURS_PER_DAY:
            raise ValueError("Insufficient complete weather model rows")
        recent = [
            measurements.get(cutoff - timedelta(hours=offset))
            for offset in range(1, RECENT_HISTORY_HOURS + 1)
        ]
        if any(value is None for value in recent):
            raise ValueError("Recent weather model history is incomplete")
        matrix = DMatrix(
            np.asarray(rows), label=targets, feature_names=list(FEATURE_NAMES), nthread=1
        )
        booster = train(
            {
                "objective": "reg:squarederror",
                "tree_method": "hist",
                "device": "cpu",
                "nthread": 1,
                "max_depth": TREE_DEPTH,
                "eta": TREE_LEARNING_RATE,
                "min_child_weight": TREE_MIN_CHILD_WEIGHT,
                "seed": 0,
            },
            matrix,
            num_boost_round=TREE_ROUNDS,
        )
        model_json = booster.save_raw(raw_format="json").decode()
        return cls(
            configuration.timezone,
            configuration.version,
            digest([(sample.start, sample.residual_kwh) for sample in samples]),
            cutoff,
            mean(recent),
            min(used_temperatures),
            max(used_temperatures),
            model_json,
            digest(model_json),
            len(samples),
            len(rows),
            tuple(excluded_periods),
        )

    @classmethod
    def restore(cls, document, configuration_version):
        """Refuse another device, feature order, schema, nonfinite metadata or corrupt native archive."""
        try:
            model = cls(
                **{
                    **document,
                    "trained_until": aware(datetime.fromisoformat(document["trained_until"])),
                    "feature_names": tuple(document["feature_names"]),
                    "excluded_periods": tuple(
                        tuple(period) for period in document["excluded_periods"]
                    ),
                }
            )
            ZoneInfo(model.timezone)
            if (
                model.schema != MODEL_SCHEMA
                or model.configuration_version != configuration_version
                or model.feature_names != FEATURE_NAMES
            ):
                raise ValueError("Incompatible weather model archive")
            quantity(model.recent_kwh)
            if (
                type(model.source_hours) is not int
                or type(model.training_hours) is not int
                or not MIN_CONTIGUOUS_RIDGE_DAYS * HOURS_PER_DAY
                <= model.training_hours
                <= model.source_hours
            ):
                raise ValueError("Invalid weather model training counts")
            excluded_hours = 0
            for start, end, reason in model.excluded_periods:
                start, end = (
                    aware(datetime.fromisoformat(start)),
                    aware(datetime.fromisoformat(end)),
                )
                if (
                    reason not in TrainingExclusionReason
                    or start >= end
                    or end > model.trained_until
                ):
                    raise ValueError("Invalid weather model training exclusion")
                excluded_hours += (end - start) / timedelta(hours=1)
            if excluded_hours != model.source_hours - model.training_hours:
                raise ValueError("Weather model training counts differ")
            if (
                not all(
                    type(value) in (int, float) and isfinite(value)
                    for value in (model.minimum_temperature, model.maximum_temperature)
                )
                or not -90 <= model.minimum_temperature <= model.maximum_temperature <= 65
            ):
                raise ValueError("Invalid weather model temperature range")
            _booster(model.model_json, model.model_digest)
            return model
        except (KeyError, TypeError, OverflowError) as error:
            raise ValueError("Invalid weather model archive") from error

    def predict(self, instant, *, temperature=None, recent=None):
        """Return finite nonnegative kWh/hour, or refuse missing inputs and weather extrapolation."""
        from xgboost import DMatrix

        if (
            instant < self.trained_until
            or type(temperature) not in (int, float)
            or not self.minimum_temperature <= temperature <= self.maximum_temperature
        ):
            raise ValueError("Weather model feature coverage is invalid")
        recent = self.recent_kwh if recent is None else recent
        quantity(recent)
        row = features(instant, self.timezone, temperature=temperature, recent=recent)
        result = float(
            _booster(self.model_json, self.model_digest).predict(
                DMatrix(np.asarray([row]), feature_names=list(FEATURE_NAMES), nthread=1)
            )[0]
        )
        if not isfinite(result):
            raise ValueError("Weather model prediction is not finite")
        return max(0.0, result)
