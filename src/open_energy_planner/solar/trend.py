"""Conservative measured-yield trends and chronological seasonal acceptance evidence.

Weather-normalized monthly residual totals estimate a multiplicative time trend.
This is an empirical yield change, not an attribution to physical degradation.
Zero-production measurements remain in both the learner and monthly totals.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import exp, isfinite, log
from statistics import median

from open_energy_planner.prices.model import aware

SOLAR_YEAR_DAYS = 365.2425
SECONDS_PER_DAY = 86400
MAX_ANNUAL_TREND_FRACTION = 0.05
MIN_MONTH_DAYS = 14
MIN_MONTH_HOURS = 28
MIN_MATCHED_MONTHS = 12
MIN_TREND_TRAINING_DAYS = 2 * SOLAR_YEAR_DAYS
TREND_VALIDATION_YEARS = 2
QUARTERS_PER_YEAR = 4
TREND_VALIDATION_FOLDS = TREND_VALIDATION_YEARS * QUARTERS_PER_YEAR
MIN_FOLD_DAYS = 28
MIN_FOLD_HOURS = 100
MIN_YEAR_RELATIVE_GAIN = 0.05
MIN_YEAR_ABSOLUTE_GAIN_KWH = 0.001
MIN_QUARTER_WINS_PER_YEAR = 3
MAX_QUARTER_REGRESSION_KWH = 0.001
MAX_ANNUAL_RATE_DEVIATION = 0.005
TREND_POLICY = "equal-weight-matched-month-trend-eight-quarter-v1"
TREND_SAFETY_ADVICE = "The measured annual yield change exceeds the safety range. Check for installation or measurement changes and set Learning data from if needed. No trend is applied."


class TrendRateOutOfRange(ValueError):
    """Distinguish sufficient data with unsafe drift from missing comparable monthly evidence."""


class TrendStatus(StrEnum):
    """Explain automatic acceptance separately from ordinary ML forecast selection."""

    ACTIVE = "active"
    INSUFFICIENT_HISTORY = "insufficient_history"
    NOT_IMPROVED = "not_improved"
    UNSTABLE = "unstable"


@dataclass(frozen=True)
class YieldTrend:
    """Annual log-rate relative to an aware reference, bounded to five percent per year."""

    log_rate: float
    reference: datetime

    def __post_init__(self):
        """Reject nonfinite, excessive or untyped archived extrapolation parameters."""
        if (
            type(self.log_rate) not in (int, float)
            or not isfinite(self.log_rate)
            or not log(1 - MAX_ANNUAL_TREND_FRACTION)
            <= self.log_rate
            <= log(1 + MAX_ANNUAL_TREND_FRACTION)
        ):
            raise ValueError("Invalid solar yield trend")
        object.__setattr__(self, "reference", aware(self.reference))

    @property
    def annual_change(self):
        """Return the learned annual multiplicative change as a fraction."""
        return exp(self.log_rate) - 1

    def factor(self, instant):
        """Scale an equal-condition yield from the reference to the requested UTC instant."""
        years = (
            (aware(instant) - self.reference).total_seconds() / SECONDS_PER_DAY / SOLAR_YEAR_DAYS
        )
        return exp(self.log_rate * years)


@dataclass(frozen=True)
class TrendFold:
    """Retain exact strictly later quarterly comparisons against equal-weight no-trend ML."""

    training_end: datetime
    validation_start: datetime
    validation_end: datetime
    baseline_mae_kwh: float
    trend_mae_kwh: float
    annual_change: float
    hours: int
    days: int


@dataclass(frozen=True)
class TrendAssessment:
    """Expose the automatic decision, attempted trend and original chronological evidence."""

    status: TrendStatus
    annual_change: float | None = None
    candidate_annual_change: float | None = None
    reference: datetime | None = None
    folds: tuple[TrendFold, ...] = ()
    reason: str = "A yield trend needs about four years of comparable monthly production, including two training years and two complete later seasonal validation years. Equal-weight ML remains available."


def estimate_trend(rows, predictions):
    """Estimate a robust same-month yield drift after the learner explains weather and season.

    Only training outcomes enter this estimate. Same calendar-month pairs avoid
    mistaking seasonal composition for ageing. Nonpositive monthly totals cannot
    identify a multiplicative rate. They are retained by the production learner.
    A rate outside the safety bound is rejected rather than clipped into evidence.
    """
    if (rows[-1][0] - rows[0][0]).total_seconds() / SECONDS_PER_DAY < MIN_TREND_TRAINING_DAYS:
        return None
    months = defaultdict(list)
    hourly = defaultdict(list)
    for row, predicted in zip(rows, predictions, strict=True):
        hourly[row[0]].append((row[2], max(0.0, float(predicted))))
    for instant, forecasts in sorted(hourly.items()):
        months[(instant.year, instant.month)].append(
            (
                instant,
                sum(value[0] for value in forecasts) / len(forecasts),
                sum(value[1] for value in forecasts) / len(forecasts),
            )
        )
    ratios = defaultdict(list)
    for (year, month), samples in sorted(months.items()):
        actual = sum(sample[1] for sample in samples)
        predicted = sum(sample[2] for sample in samples)
        if (
            len(samples) >= MIN_MONTH_HOURS
            and len({sample[0].date() for sample in samples}) >= MIN_MONTH_DAYS
            and actual > 0
            and predicted > 0
        ):
            ratios[month].append((year, log(actual / predicted)))
    matched = []
    for samples in ratios.values():
        if len(samples) >= 2:
            matched.append(
                median(
                    (later[1] - earlier[1]) / (later[0] - earlier[0])
                    for index, earlier in enumerate(samples)
                    for later in samples[index + 1 :]
                )
            )
    if len(matched) < MIN_MATCHED_MONTHS:
        return None
    rate = median(matched)
    if not log(1 - MAX_ANNUAL_TREND_FRACTION) <= rate <= log(1 + MAX_ANNUAL_TREND_FRACTION):
        raise TrendRateOutOfRange("Measured solar yield trend exceeds the safety range")
    return YieldTrend(rate, rows[-1][0])


def quarterly_windows(rows):
    """Return the eight complete calendar quarters preceding the latest training sample."""
    latest = rows[-1][0]
    end = latest.replace(
        month=(latest.month - 1) // 3 * 3 + 1, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    windows = []
    for _ in range(TREND_VALIDATION_FOLDS):
        start = end.replace(
            year=end.year - (end.month == 1), month=10 if end.month == 1 else end.month - 3
        )
        windows.append((start, end))
        end = start
    return tuple(reversed(windows))


def assess_trend(trend, folds):
    """Require improvement in both validation years and stable signs across seasonal folds."""
    if trend is None or len(folds) != TREND_VALIDATION_FOLDS:
        return TrendAssessment(TrendStatus.INSUFFICIENT_HISTORY, folds=tuple(folds))
    report = {
        "candidate_annual_change": trend.annual_change,
        "reference": trend.reference,
        "folds": tuple(folds),
    }
    if any(fold.annual_change * trend.annual_change <= 0 for fold in folds):
        return TrendAssessment(
            TrendStatus.UNSTABLE,
            **report,
            reason="The estimated yield direction changes between historical training periods. No trend is applied.",
        )
    typical_rate = median(fold.annual_change for fold in folds)
    if any(
        abs(rate - typical_rate) > MAX_ANNUAL_RATE_DEVIATION
        for rate in (trend.annual_change, *(fold.annual_change for fold in folds))
    ):
        return TrendAssessment(
            TrendStatus.UNSTABLE,
            **report,
            reason="The estimated annual yield change varies too much between historical training periods. No trend is applied.",
        )
    for year_index in range(TREND_VALIDATION_YEARS):
        year = folds[year_index * QUARTERS_PER_YEAR : (year_index + 1) * QUARTERS_PER_YEAR]
        hours = sum(fold.hours for fold in year)
        baseline = sum(fold.baseline_mae_kwh * fold.hours for fold in year) / hours
        candidate = sum(fold.trend_mae_kwh * fold.hours for fold in year) / hours
        if (
            baseline - candidate
            < max(MIN_YEAR_ABSOLUTE_GAIN_KWH, baseline * MIN_YEAR_RELATIVE_GAIN)
            or sum(fold.trend_mae_kwh < fold.baseline_mae_kwh for fold in year)
            < MIN_QUARTER_WINS_PER_YEAR
            or any(
                fold.trend_mae_kwh - fold.baseline_mae_kwh > MAX_QUARTER_REGRESSION_KWH
                for fold in year
            )
        ):
            return TrendAssessment(
                TrendStatus.NOT_IMPROVED,
                **report,
                reason="The yield trend did not consistently improve both validation years across seasons. No trend is applied.",
            )
    return TrendAssessment(
        TrendStatus.ACTIVE,
        annual_change=trend.annual_change,
        **report,
        reason="The measured yield trend improved both later validation years across seasons. It is applied automatically.",
    )
