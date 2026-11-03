"""Plain local calibration using measured production, with no physical-model replacement."""

from dataclasses import dataclass
from datetime import datetime, timedelta
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
    if source.can_curtail:
        return "curtailment_feedback_required"
    if inverter.hybrid and source.measurement_boundary == "ac":
        return "hybrid_route_history_required"
    return None


def fit(source, history, physical_hours, before, inverter):
    """Learn one bounded factor per measured source from complete, uncurtailed daylight hours.

    Cumulative Recorder sums handle meter resets. Gaps/decreasing corrections,
    clipping and potentially curtailed sources are not training targets for
    available production. Unknown historical curtailment disables calibration.
    No command is assumed physically executed. AC targets compare to physical AC.
    """
    if calibration_blocker(source, inverter) or history is None:
        return None
    predictions = {
        hour.start: hour for hour in physical_hours if hour.source_id == source.source_id
    }
    rows = []
    for previous_hour, next_hour in pairwise(history.hours):
        if (
            next_hour.start - previous_hour.start != timedelta(hours=1)
            or next_hour.start + timedelta(hours=1) > before
        ):
            continue
        actual = next_hour.total_kwh - previous_hour.total_kwh
        expected = predictions.get(next_hour.start)
        if expected is None or actual < 0:
            continue
        predicted = expected.ac_kwh if source.measurement_boundary == "ac" else expected.dc_kwh
        if (
            predicted < MIN_CALIBRATION_PRODUCTION_KWH
            or expected.ac_kwh >= inverter.ac_limit_w / WATTS_PER_KW * INVERTER_CLIPPING_THRESHOLD
            or expected.ac_kwh
            < expected.dc_kwh * inverter.efficiency * CONVERSION_CLIPPING_THRESHOLD
        ):
            continue
        rows.append((next_hour.start, predicted, actual))
    if len(rows) < MIN_CALIBRATION_SAMPLES or rows[-1][0] - rows[0][0] < timedelta(
        days=MIN_CALIBRATION_SPAN_DAYS
    ):
        return None
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
    return Calibration(source.source_id, factor, len(rows), version(rows), before)
