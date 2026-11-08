"""Bounded per-source CPU learning with chronological validation and retained cleaning evidence."""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from importlib.metadata import version as distribution_version
from itertools import pairwise
from math import cos, fsum, isfinite, pi, sin
from statistics import median, quantiles

import numpy as np
import pandas as pd
from pvlib import solarposition

from open_energy_planner.core.units import MINUTES_PER_HOUR
from open_energy_planner.planning_policy import FINE_INTERVAL_MINUTES
from open_energy_planner.prices.model import aware
from open_energy_planner.solar.calculation.model import (
    MAX_SOLAR_ENERGY_KWH,
    identity,
    number,
    version,
)
from open_energy_planner.solar.learning import (
    MIN_CALIBRATION_SAMPLES,
    TrainingExclusion,
    learning_blocker,
    unrestricted_interval,
)
from open_energy_planner.solar.model_policy import MIN_ABSOLUTE_GAIN_KWH, MIN_RELATIVE_GAIN

MODEL_ID = "solar-xgboost-production-v2"
TREE_ROUNDS = 96
TREE_DEPTH = 3
TREE_LEARNING_RATE = 0.08
TREE_MIN_CHILD_WEIGHT = 12
MAX_MODEL_JSON_BYTES = 2 * 1024 * 1024
MIN_TRAINING_DAYS = 28
VALIDATION_DAYS = 7
MIN_VALIDATION_HOURS = 28
MAX_DC_CAPACITY_MULTIPLE = 3.0
AC_METER_HEADROOM = 1.05
MIN_OUTLIER_GROUP_SAMPLES = 8
OUTLIER_SEASON_DAYS = 45
OUTLIER_IQR_MULTIPLIER = 3.0
MIN_OUTLIER_RATIO_DISTANCE = 0.15
SOLAR_YEAR_DAYS = 365.2425
FEATURE_NAMES = (
    "utc_hour_sin",
    "utc_hour_cos",
    "year_sin",
    "year_cos",
    "ghi_kw_m2",
    "dni_kw_m2",
    "dhi_kw_m2",
    "temperature_scaled",
    "wind_scaled",
    "solar_elevation",
    "solar_azimuth_sin",
    "solar_azimuth_cos",
    "daylight_fraction",
)
GEOMETRY_SUBSTEPS_PER_HOUR = MINUTES_PER_HOUR // FINE_INTERVAL_MINUTES
PREDICTION_QUARTER_HOURS = FINE_INTERVAL_MINUTES / MINUTES_PER_HOUR
HIGH_IRRADIANCE_W_M2 = 600
MIN_DIAGNOSTIC_IRRADIANCE_W_M2 = 50


class TrainingStatus(StrEnum):
    """Separate usable shadow candidates from data readiness and simpler-model preference."""

    BLOCKED = "blocked"
    INSUFFICIENT_HISTORY = "insufficient_clean_history"
    LINEAR_PREFERRED = "linear_preferred"
    NONLINEAR_SHADOW = "nonlinear_shadow"
    FAILED = "training_failed"
    INSUFFICIENT_VALIDATION = "insufficient_validation_history"
    BASELINE_PREFERRED = "baseline_preferred"


@dataclass(frozen=True)
class TrainingSummary:
    """Count source/training/validation hours and preserve excluded UTC intervals and kWh errors."""

    status: TrainingStatus
    source_hours: int
    training_hours: int
    validation_hours: int
    excluded_counts: dict
    excluded_periods: tuple
    baseline_mae_kwh: float | None = None
    linear_mae_kwh: float | None = None
    tree_mae_kwh: float | None = None
    issue: str | None = None
    validation_start: datetime | None = None
    validation_end: datetime | None = None
    training_days: int = 0
    validation_days: int = 0
    diagnostic_counts: dict | None = None
    diagnostic_periods: tuple = ()


def solar_geometry(instants, location, duration_h=1):
    """Return geometric daylight and sun direction independently of weather or PVWatts.

    Four midpoint samples per hour retain partially sunlit dawn and dusk hours.
    Values describe the UTC interval, not the provider's irradiance estimate.
    """
    if not instants:
        return {}
    steps = max(1, round(duration_h * GEOMETRY_SUBSTEPS_PER_HOUR))
    times = pd.DatetimeIndex(
        [
            instant + timedelta(hours=duration_h * (step + 0.5) / steps)
            for instant in instants
            for step in range(steps)
        ]
    )
    sun = solarposition.get_solarposition(times, location.latitude, location.longitude)
    elevations = np.asarray(sun.elevation).reshape(-1, steps)
    azimuths = np.radians(np.asarray(sun.azimuth).reshape(-1, steps))
    return {
        instant: (
            float(elevations[index].mean() / 90),
            float(np.sin(azimuths[index]).mean()),
            float(np.cos(azimuths[index]).mean()),
            float((elevations[index] > 0).mean()),
        )
        for index, instant in enumerate(instants)
    }


def _features(instant, weather, geometry, duration_h=1):
    """Describe weather and astronomical time without a production-model input."""
    hour = (instant.hour + instant.minute / 60 + duration_h / 2) / 24
    year = (instant.timetuple().tm_yday - 1 + hour) / SOLAR_YEAR_DAYS
    return (
        sin(2 * pi * hour),
        cos(2 * pi * hour),
        sin(2 * pi * year),
        cos(2 * pi * year),
        weather.ghi / 1000,
        weather.dni / 1000,
        weather.dhi / 1000,
        weather.celsius / 40,
        weather.wind_ms / 10,
        *geometry,
    )


def _validate_json(model_json):
    """Reject oversized, incompatible or cyclic trees before calling a native model loader."""
    if not isinstance(model_json, str) or len(model_json.encode()) > MAX_MODEL_JSON_BYTES:
        raise ValueError("Invalid solar model archive")
    try:
        body = json.loads(model_json)
        learner = body["learner"]
        model = learner["gradient_booster"]["model"]
        trees = model["trees"]
        if (
            body["version"] != [3, 4, 1]
            or any(type(value) is not int for value in body["version"])
            or learner["feature_names"] != list(FEATURE_NAMES)
            or learner["feature_types"] != []
            or learner["attributes"] != {}
            or learner["gradient_booster"]["name"] != "gbtree"
            or learner["objective"]["name"] != "reg:squarederror"
            or learner["learner_model_param"]["num_feature"] != str(len(FEATURE_NAMES))
            or learner["learner_model_param"]["num_class"] != "0"
            or learner["learner_model_param"]["num_target"] != "1"
            or not 1 <= len(trees) <= TREE_ROUNDS
            or model["gbtree_model_param"]
            != {"num_parallel_tree": "1", "num_trees": str(len(trees))}
            or model["iteration_indptr"] != list(range(len(trees) + 1))
            or model["tree_info"] != [0] * len(trees)
            or any(
                type(value) is not int for value in model["tree_info"] + model["iteration_indptr"]
            )
            or any(model["cats"].values())
        ):
            raise ValueError("Unsupported solar model structure")
        base = json.loads(learner["learner_model_param"]["base_score"])
        if not isinstance(base, list) or len(base) != 1:
            raise ValueError("Invalid solar model base score")
        number(base[0], 0, MAX_SOLAR_ENERGY_KWH)
        for tree_index, tree in enumerate(trees):
            node_count = tree["tree_param"]["num_nodes"]
            if node_count not in tuple(str(count) for count in range(1, 2 ** (TREE_DEPTH + 1))):
                raise ValueError("Invalid solar tree size")
            count = int(node_count)
            if (
                type(tree["id"]) is not int
                or tree["id"] != tree_index
                or tree["tree_param"]
                != {
                    "num_deleted": "0",
                    "num_feature": str(len(FEATURE_NAMES)),
                    "num_nodes": node_count,
                    "size_leaf_vector": "1",
                }
            ):
                raise ValueError("Unsupported solar tree parameters")
            for field in (
                "base_weights",
                "default_left",
                "left_children",
                "loss_changes",
                "parents",
                "right_children",
                "split_conditions",
                "split_indices",
                "split_type",
                "sum_hessian",
            ):
                if not isinstance(tree[field], list) or len(tree[field]) != count:
                    raise ValueError("Invalid solar tree arrays")
                if any(
                    type(value) not in (int, float) or not isfinite(value) for value in tree[field]
                ):
                    raise ValueError("Invalid solar tree quantity")
            if any(
                tree[field]
                for field in (
                    "categories",
                    "categories_nodes",
                    "categories_segments",
                    "categories_sizes",
                )
            ):
                raise ValueError("Categorical solar trees are unsupported")
            if tree["split_type"] != [0] * count or any(
                value not in (0, 1) for value in tree["default_left"]
            ):
                raise ValueError("Unsupported solar tree splits")
            for field in (
                "left_children",
                "right_children",
                "parents",
                "split_indices",
                "split_type",
                "default_left",
            ):
                if any(type(value) is not int for value in tree[field]):
                    raise ValueError("Invalid solar tree index")
            pending, seen = [(0, 0, 2147483647)], set()
            while pending:
                node, depth, parent = pending.pop()
                if type(node) is not int or not 0 <= node < count or node in seen:
                    raise ValueError("Invalid solar tree links")
                seen.add(node)
                if tree["parents"][node] != parent:
                    raise ValueError("Inconsistent solar tree parent")
                left, right = tree["left_children"][node], tree["right_children"][node]
                if left == right == -1:
                    continue
                if depth >= TREE_DEPTH or type(tree["split_indices"][node]) is not int:
                    raise ValueError("Invalid solar tree depth")
                if not 0 <= tree["split_indices"][node] < len(FEATURE_NAMES):
                    raise ValueError("Invalid solar tree feature")
                pending.extend(((left, depth + 1, node), (right, depth + 1, node)))
            if len(seen) != count:
                raise ValueError("Unreachable solar tree nodes")
    except (AttributeError, KeyError, TypeError, OverflowError, RecursionError) as error:
        raise ValueError("Invalid solar model archive") from error


@lru_cache(maxsize=16)
def _booster(model_json):
    """Keep bounded native prediction objects inside this adapter, never in stored documents."""
    from xgboost import Booster

    _validate_json(model_json)
    model = Booster(params={"nthread": 1, "device": "cpu"})
    model.load_model(bytearray(model_json.encode()))
    return model


@dataclass(frozen=True)
class SolarBoostedModel:
    """Retain source-bound metadata and native JSON, with no executable Python serialization."""

    source_id: str
    configuration_version: str
    location_version: str
    capacity_kw: float
    measurement_boundary: str
    max_power_kw: float
    trained_until: datetime
    created_at: datetime
    training_version: str
    library_version: str
    model_json: str
    model_digest: str
    schema: str = MODEL_ID

    def __post_init__(self):
        """Validate archive metadata and the complete bounded tree structure before prediction."""
        identity(self.source_id)
        number(self.capacity_kw, 0.001, 10000)
        number(self.max_power_kw, 0.001, 30000)
        if self.measurement_boundary not in ("ac", "dc"):
            raise ValueError("Invalid solar learning boundary")
        object.__setattr__(self, "trained_until", aware(self.trained_until))
        object.__setattr__(self, "created_at", aware(self.created_at))
        if self.trained_until > self.created_at:
            raise ValueError("Solar training cutoff is after model creation")
        if self.schema != MODEL_ID or self.library_version != "3.4.1":
            raise ValueError("Unsupported solar learning model")
        for reference in (
            self.configuration_version,
            self.location_version,
            self.training_version,
            self.model_digest,
        ):
            if (
                not isinstance(reference, str)
                or len(reference) != 64
                or any(character not in "0123456789abcdef" for character in reference)
            ):
                raise ValueError("Invalid solar model reference")
        _validate_json(self.model_json)
        if hashlib.sha256(self.model_json.encode()).hexdigest() != self.model_digest:
            raise ValueError("Solar model checksum mismatch")

    @property
    def version(self):
        """Bind a persisted model to its exact weights, training cutoff and source definition."""
        return version(asdict(self))

    @property
    def policy_version(self):
        """Keep earlier scalar or incompatible-library accuracy evidence out of tree promotion."""
        return version(
            {
                "model": self.schema,
                "library": self.library_version,
                "features": FEATURE_NAMES,
                "rounds": TREE_ROUNDS,
                "depth": TREE_DEPTH,
                "eta": TREE_LEARNING_RATE,
                "child_weight": TREE_MIN_CHILD_WEIGHT,
                "validation_days": VALIDATION_DAYS,
                "training_days": MIN_TRAINING_DAYS,
                "training_hours": MIN_CALIBRATION_SAMPLES,
                "validation_hours": MIN_VALIDATION_HOURS,
                "gain_kwh": MIN_ABSOLUTE_GAIN_KWH,
                "gain_ratio": MIN_RELATIVE_GAIN,
                "ac_headroom": AC_METER_HEADROOM,
                "dc_headroom": MAX_DC_CAPACITY_MULTIPLE,
                "target": "measured_boundary_kwh_per_hour",
                "daylight": "geometric-quarter-midpoints",
                "admission": "physical-measurement-controls-v2",
            }
        )

    @classmethod
    def restore(cls, body):
        """Decode only the known archive schema with explicit UTC training and knowledge times."""
        if not isinstance(body, dict):
            raise TypeError("Invalid solar model archive")
        try:
            return cls(
                **{
                    **body,
                    "trained_until": datetime.fromisoformat(body["trained_until"]),
                    "created_at": datetime.fromisoformat(body["created_at"]),
                }
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid solar model archive") from error

    def predict(self, weather, location, *, known_at, duration_h=1, offset_minutes=0):
        """Predict measured-boundary kWh directly without any PVWatts forecast.

        Hourly trained targets are mean kW. A finer interval uses its own sun
        position and duration. Night is physical zero. Missing weather stays absent.
        The cap is the installation's physical rating, never a ratio to a forecast.
        """
        from xgboost import DMatrix

        known_at = aware(known_at)
        if self.created_at > known_at:
            raise ValueError("Solar model was created after prediction")
        if version(asdict(location)) != self.location_version:
            raise ValueError("Solar learning location changed")
        if duration_h not in (PREDICTION_QUARTER_HOURS, 1):
            raise ValueError("Solar learning requires hourly or quarter intervals")
        if offset_minutes not in range(0, MINUTES_PER_HOUR, FINE_INTERVAL_MINUTES) or (
            duration_h == 1 and offset_minutes != 0
        ):
            raise ValueError("Invalid solar prediction offset")
        rows = tuple(row for row in weather if row.received_at <= known_at)
        offset = timedelta(minutes=offset_minutes)
        geometry = solar_geometry(tuple(row.start + offset for row in rows), location, duration_h)
        daylight = tuple(row for row in rows if geometry[row.start + offset][3] > 0)
        result = {row.start + offset: 0.0 for row in rows if geometry[row.start + offset][3] == 0}
        if daylight:
            values = _booster(self.model_json).predict(
                DMatrix(
                    np.array(
                        [
                            _features(
                                row.start + offset, row, geometry[row.start + offset], duration_h
                            )
                            for row in daylight
                        ]
                    ),
                    feature_names=list(FEATURE_NAMES),
                    nthread=1,
                )
            )
            if not np.isfinite(values).all():
                raise ValueError("Nonfinite solar learning output")
            result.update(
                {
                    row.start + offset: max(0.0, min(self.max_power_kw, float(value))) * duration_h
                    for row, value in zip(daylight, values, strict=True)
                }
            )
        return result


@dataclass(frozen=True)
class TrainingResult:
    """Return an optional nonlinear challenger separately from its complete admission report."""

    model: SolarBoostedModel | None
    summary: TrainingSummary
    archive_reference: str | None = None


@lru_cache(maxsize=16)
def _train(rows):
    """Reuse unchanged daily training data instead of fitting again on every forecast quarter."""
    from xgboost import DMatrix, train

    features = np.array([row[3] for row in rows])
    target = np.array([row[2] for row in rows])
    model = train(
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
        DMatrix(features, label=target, feature_names=list(FEATURE_NAMES), nthread=1),
        num_boost_round=TREE_ROUNDS,
    )
    return model.save_raw(raw_format="json").decode()


def _periods(excluded):
    """Coalesce adjacent rejected hours without losing their owned exclusion reason."""
    periods = []
    for start, reason in sorted(excluded):
        end = start + timedelta(hours=1)
        if periods and periods[-1][1] == start and periods[-1][2] == reason:
            periods[-1] = (periods[-1][0], end, reason)
        else:
            periods.append((start, end, reason))
    return tuple(periods)


def fit_boosted_model(source, inverter, location, history, physical_hours, weather, before):
    """Train direct measured production on clean older daylight and score a later week.

    PVWatts enters only the held-out comparison. Neither its estimates nor model
    errors select training targets. Statistical anomalies are diagnostics only.
    Raw cumulative history remains unchanged. Control declarations are bounded
    to their explicit historical UTC period and cannot authorize future outcomes.
    """
    before = aware(before)
    source_hours = max(0, len(history.hours) - 1) if history else 0
    issue = learning_blocker(source, inverter)
    if issue:
        return TrainingResult(
            None, TrainingSummary(TrainingStatus.BLOCKED, source_hours, 0, 0, {}, (), issue=issue)
        )
    predictions = {row.start: row for row in physical_hours if row.source_id == source.source_id}
    by_hour = {row.start: row for row in weather}
    instants = tuple(following.start for following in history.hours[1:]) if history else ()
    geometry = solar_geometry(instants, location)
    max_power_kw = (
        inverter.ac_limit_w / 1000
        if source.measurement_boundary == "ac"
        else source.capacity_w / 1000 * MAX_DC_CAPACITY_MULTIPLE
    )
    rows, excluded, diagnostics = [], [], []
    for previous, following in pairwise(history.hours if history else ()):
        instant = following.start
        end = instant + timedelta(hours=1)
        observed = following.total_kwh - previous.total_kwh
        conditions = by_hour.get(instant)
        reason = (
            TrainingExclusion.HISTORY_GAP
            if instant - previous.start != timedelta(hours=1)
            else TrainingExclusion.INCOMPLETE_HOUR
            if end > before
            else TrainingExclusion.METER_CORRECTION
            if observed < 0
            else "control_history_unconfirmed"
            if not unrestricted_interval(source, instant, end)
            else "outside_daylight"
            if geometry[instant][3] == 0
            else TrainingExclusion.WEATHER_MISSING
            if conditions is None
            else TrainingExclusion.WEATHER_NOT_KNOWN
            if conditions.received_at > before
            else TrainingExclusion.PRODUCTION_OUT_OF_RANGE
            if observed > max_power_kw * AC_METER_HEADROOM
            else None
        )
        if reason:
            excluded.append((instant, reason))
            continue
        expected = predictions.get(instant)
        physical = (
            (expected.ac_kwh if source.measurement_boundary == "ac" else expected.dc_kwh)
            if expected
            else None
        )
        rows.append(
            (instant, physical, observed, _features(instant, conditions, geometry[instant]))
        )
        if source.can_curtail and observed == 0 and conditions.ghi >= HIGH_IRRADIANCE_W_M2:
            diagnostics.append((instant, "possible_production_limitation"))
    validation_end = before.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = validation_end - timedelta(days=VALIDATION_DAYS)
    training = [(row[0], None, row[2], row[3]) for row in rows if row[0] < cutoff]
    validation = [row for row in rows if cutoff <= row[0] < validation_end]
    groups = defaultdict(list)
    for row in training:
        conditions = by_hour[row[0]]
        if conditions.ghi >= MIN_DIAGNOSTIC_IRRADIANCE_W_M2:
            groups[(row[0].hour, (row[0].timetuple().tm_yday - 1) // OUTLIER_SEASON_DAYS)].append(
                row
            )
    for group in groups.values():
        if len(group) < MIN_OUTLIER_GROUP_SAMPLES:
            continue
        ratios = [row[2] / by_hour[row[0]].ghi for row in group]
        quartiles = quantiles(ratios, n=4, method="inclusive")
        distance = max(
            (quartiles[2] - quartiles[0]) * OUTLIER_IQR_MULTIPLIER,
            source.capacity_w / 1000 * MIN_OUTLIER_RATIO_DISTANCE / 1000,
        )
        center = median(ratios)
        diagnostics.extend(
            (row[0], "irradiance_yield_anomaly")
            for row, ratio in zip(group, ratios, strict=True)
            if abs(ratio - center) > distance
        )
    training_days = len({row[0].date() for row in training})
    validation_days = len({row[0].date() for row in validation})
    summary = TrainingSummary(
        TrainingStatus.INSUFFICIENT_HISTORY,
        source_hours,
        len(training),
        len(validation),
        dict(Counter(reason for _instant, reason in excluded)),
        _periods(excluded),
        validation_start=cutoff,
        validation_end=validation_end,
        training_days=training_days,
        validation_days=validation_days,
        diagnostic_counts=dict(Counter(reason for _instant, reason in diagnostics)),
        diagnostic_periods=_periods(diagnostics),
    )
    if len(training) < MIN_CALIBRATION_SAMPLES or training_days < MIN_TRAINING_DAYS:
        return TrainingResult(None, summary)
    if len(validation) < MIN_VALIDATION_HOURS or validation_days < VALIDATION_DAYS:
        return TrainingResult(None, replace(summary, status=TrainingStatus.INSUFFICIENT_VALIDATION))
    if any(row[1] is None for row in validation):
        return TrainingResult(
            None,
            replace(
                summary,
                status=TrainingStatus.INSUFFICIENT_VALIDATION,
                issue="Historical model comparison needs matching PVWatts coverage.",
            ),
        )
    baseline_error = fsum(abs(row[1] - row[2]) for row in validation) / len(validation)
    model_json = _train(tuple(training))
    model = SolarBoostedModel(
        source.source_id,
        version({"source": asdict(source), "inverter": asdict(inverter)}),
        version(asdict(location)),
        source.capacity_w / 1000,
        source.measurement_boundary,
        max_power_kw,
        training[-1][0] + timedelta(hours=1),
        before,
        version(training),
        distribution_version("xgboost-cpu"),
        model_json,
        hashlib.sha256(model_json.encode()).hexdigest(),
    )
    from xgboost import DMatrix

    predictions = _booster(model_json).predict(
        DMatrix(
            np.array([row[3] for row in validation]), feature_names=list(FEATURE_NAMES), nthread=1
        )
    )
    if not np.isfinite(predictions).all():
        raise ValueError("Nonfinite solar learning validation")
    tree_error = fsum(
        abs(max(0.0, min(max_power_kw, float(predicted))) - row[2])
        for row, predicted in zip(validation, predictions, strict=True)
    ) / len(validation)
    preferred = baseline_error - tree_error >= max(
        MIN_ABSOLUTE_GAIN_KWH, baseline_error * MIN_RELATIVE_GAIN
    )
    summary = replace(
        summary,
        status=TrainingStatus.NONLINEAR_SHADOW if preferred else TrainingStatus.BASELINE_PREFERRED,
        baseline_mae_kwh=baseline_error,
        tree_mae_kwh=tree_error,
    )
    return TrainingResult(model if preferred else None, summary)
