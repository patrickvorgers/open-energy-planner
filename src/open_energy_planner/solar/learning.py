"""Plain local calibration using measured production, with no physical-model replacement."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from open_energy_planner.core.units import WATTS_PER_KW
from open_energy_planner.solar.calculation.model import version

MIN_CALIBRATION_FACTOR = 0.5
MIN_CALIBRATION_SAMPLES = 100
MAX_CALIBRATION_FACTOR = 1.5
MIN_CALIBRATION_PRODUCTION_KWH = 0.05
INVERTER_CLIPPING_THRESHOLD = 0.98
CONVERSION_CLIPPING_THRESHOLD = 0.999
MIN_CALIBRATION_SPAN_DAYS = 27


class TrainingExclusion(StrEnum):
    """Explain why a measured solar interval cannot train an available-production model."""

    HISTORY_GAP = "history_gap"
    INCOMPLETE_HOUR = "incomplete_hour"
    PHYSICAL_FORECAST_MISSING = "physical_forecast_missing"
    METER_CORRECTION = "meter_correction"
    LOW_PRODUCTION = "low_production"
    CLIPPING = "clipping"
    WEATHER_MISSING = "weather_missing"
    WEATHER_NOT_KNOWN = "weather_not_known"
    PRODUCTION_OUT_OF_RANGE = "production_out_of_range"
    STATISTICAL_OUTLIER = "statistical_outlier"


def training_sample_issue(source, inverter, previous, following, expected, before):
    """Share reset-safe hourly admission between scalar and nonlinear learning."""
    if following.start - previous.start != timedelta(hours=1):
        return TrainingExclusion.HISTORY_GAP
    if following.start + timedelta(hours=1) > before:
        return TrainingExclusion.INCOMPLETE_HOUR
    if expected is None:
        return TrainingExclusion.PHYSICAL_FORECAST_MISSING
    if following.total_kwh - previous.total_kwh < 0:
        return TrainingExclusion.METER_CORRECTION
    predicted = expected.ac_kwh if source.measurement_boundary == "ac" else expected.dc_kwh
    if predicted < MIN_CALIBRATION_PRODUCTION_KWH:
        return TrainingExclusion.LOW_PRODUCTION
    if (
        expected.ac_kwh >= inverter.ac_limit_w / WATTS_PER_KW * INVERTER_CLIPPING_THRESHOLD
        or expected.ac_kwh < expected.dc_kwh * inverter.efficiency * CONVERSION_CLIPPING_THRESHOLD
    ):
        return TrainingExclusion.CLIPPING
    return None


@dataclass(frozen=True)
class Calibration:
    """Regularized through-origin regression. Candidate parameters are archived without pickle."""

    source_id: str
    factor: float
    samples: int
    training_version: str
    trained_until: datetime
    status: str = "waiting_for_accuracy_evidence"
    configuration_version: str = ""
    weather_version: str = ""
    history_version: str = ""


def calibration_blocker(source, inverter):
    """Explain missing route/control evidence identically for fitting and published status."""
    if inverter.hybrid and source.measurement_boundary == "ac":
        return "hybrid_route_history_required"
    return None


def fit(source, history, physical_hours, before, inverter, *, feedback=None):
    """Learn one bounded factor per measured source from complete, uncurtailed daylight hours.

    Cumulative Recorder sums handle meter resets. Gaps/decreasing corrections,
    clipping and observed curtailment are not training targets for available
    production. Linked feedback must cover each hour after its first observation.
    No command is assumed physically executed. AC targets compare to physical AC.
    """
    if calibration_blocker(source, inverter) or history is None:
        return None
    predictions = {
        hour.start: hour for hour in physical_hours if hour.source_id == source.source_id
    }
    rows = []
    for previous_hour, next_hour in pairwise(history.hours):
        if not unrestricted_interval(
            source, next_hour.start, next_hour.start + timedelta(hours=1), feedback
        ):
            continue
        expected = predictions.get(next_hour.start)
        if training_sample_issue(source, inverter, previous_hour, next_hour, expected, before):
            continue
        actual = next_hour.total_kwh - previous_hour.total_kwh
        predicted = expected.ac_kwh if source.measurement_boundary == "ac" else expected.dc_kwh
        rows.append((next_hour.start, predicted, actual))
    if len(rows) < MIN_CALIBRATION_SAMPLES or rows[-1][0] - rows[0][0] < timedelta(
        days=MIN_CALIBRATION_SPAN_DAYS
    ):
        return None
    factor = calibration_factor(rows)
    return Calibration(source.source_id, factor, len(rows), version(rows), before)


def calibration_factor(rows):
    """Fit the shared bounded scalar prior from admitted time/predicted/measured triples only."""
    # A small prior around factor=1 avoids extreme corrections from low yield.
    factor = (
        sum(
            predicted_energy_kwh * measured_energy_kwh
            for _, predicted_energy_kwh, measured_energy_kwh in rows
        )
        + 1
    ) / (
        sum(predicted_energy_kwh * predicted_energy_kwh for _, predicted_energy_kwh, _ in rows) + 1
    )
    factor = max(MIN_CALIBRATION_FACTOR, min(MAX_CALIBRATION_FACTOR, factor))
    return factor


def learning_blocker(source, inverter):
    """Require explicit historical control evidence without inferring available hybrid DC."""
    if inverter.hybrid and source.measurement_boundary == "ac":
        return "hybrid_route_history_required"
    return None


def unrestricted_interval(source, start, end, feedback=None):
    """Assume no curtailment without a sensor, then follow actual source-bound feedback."""
    if source.curtailment_entity_id is None:
        return True
    if feedback is None or feedback.entity_id != source.curtailment_entity_id:
        return False
    return feedback.unrestricted(start, end)


def learning_interval(source, start, end):
    """Use only complete intervals beginning at or after the explicitly selected learning boundary.

    The preceding cumulative endpoint remains available for subtraction. Older
    measurements stay archived and do not enter fitting or accuracy assessment.
    """
    return end > start and (
        source.learning_start is None or start >= datetime.fromisoformat(source.learning_start)
    )
