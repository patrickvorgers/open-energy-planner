"""Identify room heat loss and effective electrical heating response from aligned evidence."""

from dataclasses import dataclass
from datetime import timedelta
from itertools import pairwise
from math import exp, expm1, isfinite
from statistics import mean

from open_energy_planner.appliances.thermal.applications import MAXIMUM_SPACE_TEMPERATURE_C
from open_energy_planner.appliances.thermal.model import (
    THERMAL_HEAT_CAPACITY_BOUNDS_KWH_PER_C,
    ThermalWork,
)
from open_energy_planner.core.units import WATTS_PER_KW

HOUR = timedelta(hours=1)
INDOOR_HISTORY_HOURS = 24 * 30
HOLDOUT_HOURS = 24
MINIMUM_TRAINING_HOURS = 48
MINIMUM_HEATING_HOURS = 8
MINIMUM_IDLE_HOURS = 8
IDLE_KWH = 0.05
SUSTAINED_HEATING_KWH = 0.2
MAXIMUM_HOLDOUT_MEAN_ERROR_C = 0.30
MAXIMUM_HOLDOUT_ERROR_C = 0.75
MINIMUM_IMPROVEMENT_C = 0.08
LOSS_RATES_PER_HOUR = tuple(rate / 1000 for rate in range(5, 201, 5))
EFFECTIVE_GAIN_BOUNDS_C_PER_KWH = (0.15, 4.0)
HOURLY_METER_TOLERANCE_KWH = 0.01


@dataclass(frozen=True)
class RoomModel:
    """Bounded first-order dynamics and independently specified running power.

    Gain describes temperature change per electrical kWh. It folds thermal mass and
    efficiency together because the electricity meter cannot identify COP separately.
    """

    loss_per_hour: float
    gain_c_per_kwh: float
    heater_w: int
    coldest_c: float
    warmest_c: float
    holdout_error_c: float


def decay_response(loss, duration):
    """Return passive retention and the exact response to constant interval heating."""
    exponent = loss * duration
    return exp(-exponent), -expm1(-exponent) / exponent


def aligned_room_samples(indoor, outdoor, energy_history, now, maximum_hourly_kwh):
    """Join complete metered hours with indoor and outdoor readings and check the power bound."""
    weather = {row.start: row.celsius for row in outdoor}
    samples = []
    for before, after in pairwise(energy_history.hours):
        start = after.start
        if (
            start < now - INDOOR_HISTORY_HOURS * HOUR
            or before.start + HOUR != start
            or start + HOUR > now
            or start not in weather
            or start not in indoor
            or start + HOUR not in indoor
        ):
            continue
        energy = after.total_kwh - before.total_kwh
        # Even an hour of continuous operation cannot exceed the configured input.
        if energy > maximum_hourly_kwh:
            return []
        if 0 <= energy:
            samples.append((indoor[start], weather[start], energy, indoor[start + HOUR]))
    return samples


def fit_room(indoor, outdoor, energy_history, now, *, full_load_power_w):
    """Learn the thermal response from hourly energy with separately known electrical input."""
    if not energy_history or full_load_power_w is None:
        return None
    maximum_hourly_kwh = full_load_power_w / WATTS_PER_KW + HOURLY_METER_TOLERANCE_KWH
    samples = aligned_room_samples(indoor, outdoor, energy_history, now, maximum_hourly_kwh)
    if len(samples) < MINIMUM_TRAINING_HOURS + HOLDOUT_HOURS:
        return None
    training, held_out = samples[:-HOLDOUT_HOURS], samples[-HOLDOUT_HOURS:]
    idle = [row for row in training if row[2] <= IDLE_KWH and row[0] - row[1] >= 3]
    if (
        len(idle) < MINIMUM_IDLE_HOURS
        or sum(row[2] >= SUSTAINED_HEATING_KWH for row in training) < MINIMUM_HEATING_HOURS
        or sum(row[2] >= SUSTAINED_HEATING_KWH for row in held_out) < 3
        or sum(row[2] <= IDLE_KWH for row in held_out) < 3
    ):
        return None

    def passive(row, retention):
        """Predict end temperature with outdoor heat exchange alone."""
        return row[1] + (row[0] - row[1]) * retention

    loss = min(
        LOSS_RATES_PER_HOUR,
        key=lambda candidate: sum((row[3] - passive(row, exp(-candidate))) ** 2 for row in idle),
    )
    if loss in (LOSS_RATES_PER_HOUR[0], LOSS_RATES_PER_HOUR[-1]):
        return None
    retention, response = decay_response(loss, 1)
    gain = sum((row[3] - passive(row, retention)) * row[2] for row in training) / (
        response * sum(row[2] ** 2 for row in training)
    )
    if (
        not isfinite(gain)
        or not EFFECTIVE_GAIN_BOUNDS_C_PER_KWH[0] <= gain <= EFFECTIVE_GAIN_BOUNDS_C_PER_KWH[1]
    ):
        return None
    errors = [abs(row[3] - passive(row, retention) - response * gain * row[2]) for row in held_out]
    mean_error = mean(errors)
    if (
        mean_error > MAXIMUM_HOLDOUT_MEAN_ERROR_C
        or max(errors) > MAXIMUM_HOLDOUT_ERROR_C
        or mean(abs(row[3] - passive(row, retention)) for row in held_out) - mean_error
        < MINIMUM_IMPROVEMENT_C
    ):
        return None
    return RoomModel(
        loss,
        gain,
        full_load_power_w,
        min(row[1] for row in training),
        max(row[1] for row in training),
        max(errors),
    )


def room_work(application, reading, model, weather, intervals, now, ready):
    """Construct calibrated work only inside the observed weather and temperature envelope."""
    target = reading["target_c"]
    maximum = target + application["maximum_preheat_c"]
    model_maximum = maximum - model.holdout_error_c
    initial = reading["temperature_c"]
    capacity = 1 / model.gain_c_per_kwh
    loss = model.loss_per_hour * capacity * WATTS_PER_KW
    final = intervals[-1]
    if final.start + timedelta(hours=final.duration_h) <= ready:
        return None
    if maximum > MAXIMUM_SPACE_TEMPERATURE_C or model_maximum <= target or initial > model_maximum:
        return None
    min_capacity, max_capacity = THERMAL_HEAT_CAPACITY_BOUNDS_KWH_PER_C
    if not min_capacity <= capacity <= max_capacity or loss > 1000:
        return None
    ambient = tuple(
        weather.get(step.start.replace(minute=0, second=0, microsecond=0)) for step in intervals
    )
    if any(
        value is None or not model.coldest_c <= value <= min(model.warmest_c, target)
        for value in ambient
    ):
        return None
    passive = initial
    for step, outside in zip(intervals, ambient, strict=True):
        passive = outside + (passive - outside) * exp(-model.loss_per_hour * step.duration_h)
        if passive < 0:
            return None
    return ThermalWork(
        application["id"],
        ready,
        initial,
        target,
        model_maximum,
        0,
        capacity,
        loss,
        model.heater_w,
        1,
        (0.0,) * len(intervals),
        target,
        state_at=now,
        ambient_profile_c=ambient,
    )
