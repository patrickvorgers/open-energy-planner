"""Chronological forecast comparison through an independent planning/realized-settlement port."""

from dataclasses import dataclass
from datetime import timedelta
from itertools import pairwise
from math import isfinite
from typing import Protocol

from open_energy_planner.demand.calculation.forecasting import (
    RidgeDemandModel,
    WeekdayProfile,
    predict,
)
from open_energy_planner.demand.calculation.model import digest

from ..forecast_selection.policy import (
    MIN_ABSOLUTE_DECISION_BENEFIT_CURRENCY,
    MIN_DECISION_EVALUATION_WINDOWS,
    MIN_RELATIVE_DECISION_BENEFIT_FRACTION,
)
from ..planning_policy import PLANNING_HORIZON_HOURS


def evaluation_cutoffs(last):
    """Choose disjoint historical windows from the configured executable horizon."""
    return tuple(
        last - timedelta(hours=PLANNING_HORIZON_HOURS * window)
        for window in range(MIN_DECISION_EVALUATION_WINDOWS, 0, -1)
    )


class DecisionScorer(Protocol):
    """Hold historical prices, PV, installation limits and initial inventory fixed for both models."""

    def cost(self, forecast, actual):
        """Plan with the forecast, independently validate and settle against actual residual demand.

        Return realized economic cost, including wear/inventory where relevant.
        Raise when required historical decision inputs are unavailable. Forecast
        MAE and forecast objective cost alone do not satisfy this contract.
        """
        ...


@dataclass(frozen=True)
class DecisionComparison:
    """Measured decision benefit for disjoint historical planning windows, not a user approval flag."""

    configuration_version: str
    data_version: str
    temperature_enabled: bool
    windows: tuple
    baseline_costs: tuple[float, ...]
    ridge_costs: tuple[float, ...]

    @property
    def improved(self):
        """Require at least three consistently cheaper windows with a nontrivial absolute margin."""
        return len(self.windows) >= MIN_DECISION_EVALUATION_WINDOWS and all(
            baseline - candidate
            > max(
                MIN_ABSOLUTE_DECISION_BENEFIT_CURRENCY,
                MIN_RELATIVE_DECISION_BENEFIT_FRACTION * abs(baseline),
            )
            for baseline, candidate in zip(self.baseline_costs, self.ridge_costs, strict=True)
        )


def compare(configuration, samples, cutoffs, scorer: DecisionScorer, *, temperatures=()):
    """Refit before each decision. Never expose future load/weather while constructing forecasts."""
    if len(cutoffs) < MIN_DECISION_EVALUATION_WINDOWS or any(
        next_item - previous_item < timedelta(hours=PLANNING_HORIZON_HOURS)
        for previous_item, next_item in pairwise(cutoffs)
    ):
        raise ValueError("At least three disjoint chronological evaluation windows are required")
    baseline_costs, ridge_costs, modes = [], [], set()
    for cutoff in cutoffs:
        train = tuple(sample for sample in samples if sample.start + timedelta(hours=1) <= cutoff)
        actual = tuple(
            sample
            for sample in samples
            if cutoff <= sample.start < cutoff + timedelta(hours=PLANNING_HORIZON_HOURS)
        )
        if len(actual) != PLANNING_HORIZON_HOURS or any(
            actual_item.start != cutoff + timedelta(hours=index)
            for index, actual_item in enumerate(actual)
        ):
            raise ValueError("Evaluation requires complete future realization")
        known_weather = tuple(
            temperature for temperature in temperatures if temperature.known_at <= cutoff
        )
        ridge = RidgeDemandModel.fit(configuration, train, temperatures=known_weather)
        modes.add(ridge.temperature_enabled)
        for model, costs in (
            (WeekdayProfile.fit(configuration, train), baseline_costs),
            (ridge, ridge_costs),
        ):
            forecast = predict(configuration, train, cutoff, model, temperatures=known_weather)
            value = scorer.cost(forecast, actual)
            if type(value) not in (int, float) or not isfinite(value):
                raise ValueError("Invalid decision evaluation")
            costs.append(value)
    if len(modes) != 1:
        raise ValueError("Weather feature modes differ between evaluation windows")
    return DecisionComparison(
        configuration.version,
        digest([(sample.start, sample.total_kwh, sample.residual_kwh) for sample in samples]),
        modes.pop(),
        tuple(cutoffs),
        tuple(baseline_costs),
        tuple(ridge_costs),
    )
