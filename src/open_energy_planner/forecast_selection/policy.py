"""Shared decision-benefit thresholds. Forecasting accuracy alone never selects a model."""

from open_energy_planner.core.units import MINUTES_PER_HOUR

from ..planning_policy import FINE_INTERVAL_MINUTES, PLANNING_HORIZON_HOURS

SELECTION_POLICY = "financial-shadow-v1"
# Economic costs use the contract currency (EUR for currently supported contracts).
MIN_ABSOLUTE_DECISION_BENEFIT_CURRENCY = 0.001
MIN_RELATIVE_DECISION_BENEFIT_FRACTION = 0.01
MIN_DECISION_EVALUATION_WINDOWS = 3
MIN_DECISION_WINDOW_HOURS = PLANNING_HORIZON_HOURS - 2 * FINE_INTERVAL_MINUTES / MINUTES_PER_HOUR

MAX_REALIZATION_FRAMES = PLANNING_HORIZON_HOURS * MINUTES_PER_HOUR // FINE_INTERVAL_MINUTES
COMPARISON_SCENARIOS = ("baseline", "candidate")
