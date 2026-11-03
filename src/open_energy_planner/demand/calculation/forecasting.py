"""Lightweight local demand profiles and regularized regression using plain persisted coefficients."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import cos, pi, sin
from statistics import mean
from zoneinfo import ZoneInfo

import numpy as np

from open_energy_planner.core.forecast_timing import limited_slope
from open_energy_planner.core.units import (
    DAYS_PER_WEEK,
    HOURS_PER_DAY,
    MINUTES_PER_HOUR,
    SECONDS_PER_HOUR,
)
from open_energy_planner.demand.calculation.model import (
    DemandForecast,
    ForecastModel,
    ForecastStep,
    digest,
    horizon,
)

MIN_PROFILE_TRAINING_DAYS = 7
MIN_RIDGE_TRAINING_DAYS = 28
MIN_CONTIGUOUS_RIDGE_DAYS = MIN_RIDGE_TRAINING_DAYS - 1
RECENT_HISTORY_HOURS = 24
ANNUAL_CONSUMPTION_DAYS = 365
TROPICAL_YEAR_DAYS = 365.2425
DEFAULT_RIDGE_ALPHA = 10.0
MIN_FEATURE_SCALE = 1e-10
TEMPERATURE_SCALE_CELSIUS = 20
HEATING_BALANCE_CELSIUS = 18
COOLING_BALANCE_CELSIUS = 22
DAILY_HARMONICS = (1, 2, 3)
STARTUP_HOURLY_WEIGHTS = (0.6,) * 6 + (1.2,) * 3 + (0.9,) * 8 + (1.5,) * 5 + (0.9,) * 2


@dataclass(frozen=True)
class WeekdayProfile:
    """Weekday/hour mean with hour/global fallback, or a declared annual-demand startup shape."""

    annual_kwh: float
    timezone: str
    cells: tuple[tuple[int, int, float], ...] = ()
    fallback_kwh: float | None = None

    @property
    def model_id(self):
        """Distinguish a measured historical profile from its annual-estimate fallback."""
        return "weekday-hour-v2" if self.cells else "startup-v2"

    @property
    def residual_profile(self):
        """Claim exclusion-aware prediction only once usable residual history exists."""
        return bool(self.cells)

    @classmethod
    def fit(cls, configuration, samples):
        """Learn only measured residual samples supplied by a strictly historical caller."""
        zone = ZoneInfo(configuration.timezone)
        groups = {}
        for sample in samples:
            local = sample.start.astimezone(zone)
            groups.setdefault((local.weekday(), local.hour), []).append(sample.residual_kwh)
        if len(samples) < MIN_PROFILE_TRAINING_DAYS * HOURS_PER_DAY or {
            hour for _, hour in groups
        } != set(range(HOURS_PER_DAY)):
            return cls(configuration.annual_kwh, configuration.timezone)
        return cls(
            configuration.annual_kwh,
            configuration.timezone,
            tuple((day, hour, mean(values)) for (day, hour), values in sorted(groups.items())),
            mean(sample.residual_kwh for sample in samples) if samples else None,
        )

    def predict(self, instant, *, temperature=None, recent=None):
        """Return expected kWh/hour. The startup shape has unit daily mean and no claimed accuracy."""
        local = instant.astimezone(ZoneInfo(self.timezone))
        values = {(day, hour): value for day, hour, value in self.cells}
        if (local.weekday(), local.hour) in values:
            return values[local.weekday(), local.hour]
        same_hour = [value for _, hour, value in self.cells if hour == local.hour]
        if same_hour:
            return mean(same_hour)
        if self.fallback_kwh is not None:
            return self.fallback_kwh
        return (
            self.annual_kwh
            / ANNUAL_CONSUMPTION_DAYS
            / sum(STARTUP_HOURLY_WEIGHTS)
            * STARTUP_HOURLY_WEIGHTS[local.hour]
        )


def features(instant, timezone, *, temperature=None, recent=0):
    """Use local clock/week/season, prior-day mean and optional bounded temperature effects."""
    local = instant.astimezone(ZoneInfo(timezone))
    daily = 2 * pi * (local.hour + local.minute / MINUTES_PER_HOUR) / HOURS_PER_DAY
    yearly = 2 * pi * (local.timetuple().tm_yday - 1) / TROPICAL_YEAR_DAYS
    result = [1.0]
    for harmonic in DAILY_HARMONICS:
        result.extend((sin(harmonic * daily), cos(harmonic * daily)))
    result.extend(float(local.weekday() == day) for day in range(DAYS_PER_WEEK - 1))
    result.extend((sin(yearly), cos(yearly), recent))
    if temperature is not None:
        result.extend(
            (
                temperature / TEMPERATURE_SCALE_CELSIUS,
                max(0, HEATING_BALANCE_CELSIUS - temperature) / TEMPERATURE_SCALE_CELSIUS,
                max(0, temperature - COOLING_BALANCE_CELSIUS) / TEMPERATURE_SCALE_CELSIUS,
            )
        )
    return result


@dataclass(frozen=True)
class RidgeDemandModel:
    """L2 regression. Stored tuples contain no NumPy/native estimator objects or executable pickle."""

    timezone: str
    coefficients: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    temperature_enabled: bool
    trained_until: datetime
    recent_kwh: float
    model_id = "ridge-v2"
    residual_profile = True

    @classmethod
    def fit(cls, configuration, samples, *, temperatures=(), alpha=DEFAULT_RIDGE_ALPHA):
        """Fit standardized historical features with an unpenalized intercept via augmented least squares.

        Each training row's recent mean uses only the preceding 24 complete hours.
        At least 28 days' worth of samples is required. Missing weather disables
        weather features for the complete model rather than inventing zero degrees.
        """
        if len(samples) < MIN_RIDGE_TRAINING_DAYS * HOURS_PER_DAY or alpha <= 0:
            raise ValueError("Insufficient history or invalid regularization")
        weather = {item.start: item.celsius for item in temperatures}
        use_weather = all(sample.start in weather for sample in samples[RECENT_HISTORY_HOURS:])
        rows, targets = [], []
        by_time = {sample_item.start: sample_item.residual_kwh for sample_item in samples}
        for sample in samples:
            prior = [
                by_time.get(sample.start - timedelta(hours=index))
                for index in range(1, RECENT_HISTORY_HOURS + 1)
            ]
            if any(value is None for value in prior):
                continue
            rows.append(
                features(
                    sample.start,
                    configuration.timezone,
                    temperature=weather[sample.start] if use_weather else None,
                    recent=mean(prior),
                )
            )
            targets.append(sample.residual_kwh)
        if len(rows) < MIN_CONTIGUOUS_RIDGE_DAYS * HOURS_PER_DAY:
            raise ValueError("Insufficient contiguous history")
        matrix = np.asarray(rows, dtype=float)
        centers, scales = matrix.mean(axis=0), matrix.std(axis=0)
        centers[0], scales[0] = 0, 1
        scales[scales < MIN_FEATURE_SCALE] = 1
        matrix = (matrix - centers) / scales
        penalty = np.eye(matrix.shape[1]) * np.sqrt(alpha)
        penalty[0, 0] = 0
        coefficients = np.linalg.lstsq(
            np.vstack((matrix, penalty)),
            np.concatenate((targets, np.zeros(matrix.shape[1]))),
            rcond=None,
        )[0]
        return cls(
            configuration.timezone,
            tuple(float(coefficient) for coefficient in coefficients),
            tuple(float(center_item) for center_item in centers),
            tuple(float(scale_item) for scale_item in scales),
            use_weather,
            samples[-1].start + timedelta(hours=1),
            mean(source.residual_kwh for source in samples[-RECENT_HISTORY_HOURS:]),
        )

    def predict(self, instant, *, temperature=None, recent=None):
        """Predict nonnegative residual kWh/hour, rejecting missing required weather or pre-training forecasts."""
        if instant < self.trained_until or (self.temperature_enabled and temperature is None):
            raise ValueError("Model feature coverage or chronology is invalid")
        row = features(
            instant,
            self.timezone,
            temperature=temperature if self.temperature_enabled else None,
            recent=self.recent_kwh if recent is None else recent,
        )
        return max(
            0.0,
            sum(
                coefficient * (value_item - center) / scale
                for coefficient, value_item, center, scale in zip(
                    self.coefficients, row, self.means, self.scales, strict=True
                )
            ),
        )


def predict(configuration, samples, now, model: ForecastModel, *, temperatures=()):
    """Integrate bounded within-hour rate estimates, preserving hourly energy and known-at chronology.

    Hourly model means remain authoritative. A limited linear slope estimates
    quarter timing. It is not measured or learned sub-hour precision. No model
    request precedes the forecast window or requires weather outside it.
    """
    weather = {item.start: item.celsius for item in temperatures if item.known_at <= now}
    grid = horizon(now)
    final = grid[-1][0] + timedelta(hours=grid[-1][1])
    hour = grid[0][0].replace(minute=0, second=0, microsecond=0)
    rates = {}
    while hour < final:
        rates[hour] = model.predict(hour, temperature=weather.get(hour))
        hour += timedelta(hours=1)
    slopes = {}
    for hour, rate in rates.items():
        slopes[hour] = limited_slope(
            rates.get(hour - timedelta(hours=1), rate),
            rate,
            rates.get(hour + timedelta(hours=1), rate),
        )
    steps = []
    for start, duration in grid:
        end = start + timedelta(hours=duration)
        cursor, energy = start, 0.0
        while cursor < end:
            hour = cursor.replace(minute=0, second=0, microsecond=0)
            stop = min(end, hour + timedelta(hours=1))
            first_fraction = (cursor - hour).total_seconds() / SECONDS_PER_HOUR
            last_fraction = (stop - hour).total_seconds() / SECONDS_PER_HOUR
            energy += (
                rates[hour] * (last_fraction - first_fraction)
                + slopes[hour] * ((last_fraction - 0.5) ** 2 - (first_fraction - 0.5) ** 2) / 2
            )
            cursor = stop
        steps.append(ForecastStep(start, duration, energy))
    residual_profile = model.residual_profile
    return DemandForecast(
        configuration.version,
        digest(
            [
                (sample_item.start, sample_item.total_kwh, sample_item.residual_kwh)
                for sample_item in samples
            ]
        ),
        now,
        model.model_id,
        tuple(steps),
        tuple(item.device_id for item in configuration.exclusions) if residual_profile else (),
        digest([(item.start, item.celsius, item.known_at) for item in temperatures]),
        "total_household"
        if not residual_profile and configuration.exclusions
        else "residual_household",
        "estimated" if residual_profile else "provisional",
    )
