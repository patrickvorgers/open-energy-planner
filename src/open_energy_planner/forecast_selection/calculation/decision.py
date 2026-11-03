"""Evaluate chronological financial benefit without reading or writing stored evidence."""

from datetime import datetime
from itertools import pairwise
from math import isfinite

from open_energy_planner.core.units import SECONDS_PER_HOUR
from open_energy_planner.forecast_selection.policy import (
    MIN_ABSOLUTE_DECISION_BENEFIT_CURRENCY,
    MIN_DECISION_EVALUATION_WINDOWS,
    MIN_DECISION_WINDOW_HOURS,
    MIN_RELATIVE_DECISION_BENEFIT_FRACTION,
)
from open_energy_planner.optimization.calculation.consumption import (
    CONSUMPTION_PRIORITY_TOLERANCE_KWH,
)
from open_energy_planner.optimization.domain import Strategy


def learned_is_better(windows, strategy):
    """Decide whether the learned forecast beats the baseline in every recent window.

    Args:
        windows: Chronological records with timezone-aware ISO 8601 start/end
            strings and baseline/candidate score mappings. Each mapping holds
            finite economic_cost in contract currency and corrected_import_kwh
            in kWh. Supported contracts use EUR.
        strategy: Planning objective. Consumption also limits candidate import
            increases to CONSUMPTION_PRIORITY_TOLERANCE_KWH.

    Returns:
        True only when each of the latest MIN_DECISION_EVALUATION_WINDOWS has
        baseline cost minus candidate cost above the larger absolute or relative
        benefit threshold. Too few windows or insufficient benefit returns False.

    Raises:
        ValueError: Selected windows are naive, unordered, overlapping, shorter
            than MIN_DECISION_WINDOW_HOURS or have non-numeric/non-finite scores.
    """
    if len(windows) < MIN_DECISION_EVALUATION_WINDOWS:
        return False
    chosen = windows[-MIN_DECISION_EVALUATION_WINDOWS:]
    bounds = [
        (datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])) for row in chosen
    ]
    if any(
        start.utcoffset() is None or end.utcoffset() is None or start >= end
        for start, end in bounds
    ) or any(previous[1] > following[0] for previous, following in pairwise(bounds)):
        raise ValueError("Decision windows must be chronological and nonoverlapping")
    if any(
        (end - start).total_seconds() / SECONDS_PER_HOUR < MIN_DECISION_WINDOW_HOURS
        for start, end in bounds
    ):
        raise ValueError("Decision window is shorter than the near-horizon minimum")
    for window in chosen:
        baseline, candidate = window["baseline"], window["candidate"]
        if any(
            type(score.get(key)) not in (int, float) or not isfinite(score[key])
            for score in (baseline, candidate)
            for key in ("economic_cost", "corrected_import_kwh")
        ):
            raise ValueError("Model decision evidence must be finite")
        if baseline["economic_cost"] - candidate["economic_cost"] <= max(
            MIN_ABSOLUTE_DECISION_BENEFIT_CURRENCY,
            MIN_RELATIVE_DECISION_BENEFIT_FRACTION * abs(baseline["economic_cost"]),
        ):
            return False
        if (
            strategy == Strategy.CONSUMPTION
            and candidate["corrected_import_kwh"]
            > baseline["corrected_import_kwh"] + CONSUMPTION_PRIORITY_TOLERANCE_KWH
        ):
            return False
    return True
