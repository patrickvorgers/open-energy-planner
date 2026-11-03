"""Immutable demand contracts. No HA, HTTP, storage or numerical-library objects."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from math import isfinite
from typing import Protocol
from zoneinfo import ZoneInfo

from open_energy_planner.core.units import MINUTES_PER_HOUR
from open_energy_planner.planning_policy import (
    COARSE_INTERVAL_MINUTES,
    FINE_INTERVAL_MINUTES,
    planning_grid,
)
from open_energy_planner.prices.model import aware

MAX_ANNUAL_CONSUMPTION_KWH = 1000000.0
MAX_ENTITY_ID_LENGTH = 128
MAX_DEVICE_ID_LENGTH = 64
MAX_ENERGY_CHANNELS = 16
MAX_TEMPERATURE_CELSIUS = 65
MIN_TEMPERATURE_MAGNITUDE_CELSIUS = 90
DEFAULT_READING_DURATION_HOURS = 0.25


class ForecastModel(Protocol):
    """Replace a local predictor without coupling horizon projection to a concrete ML library."""

    @property
    def model_id(self) -> str:
        """Identify the algorithm/version retained with every prediction."""
        ...

    @property
    def residual_profile(self) -> bool:
        """Indicate whether predictions incorporate measured exclusion subtraction."""
        ...

    def predict(self, instant, *, temperature=None, recent=None) -> float:
        """Return an estimated hourly kWh rate with explicitly available features."""
        ...


def digest(value):
    """Identify normalized configuration, training evidence or forecasts without mutable pointers."""
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()


def quantity(value, *, maximum=MAX_ANNUAL_CONSUMPTION_KWH):
    """Require a finite bounded nonnegative energy or annual-demand quantity."""
    if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= maximum:
        raise ValueError("Invalid demand quantity")


@dataclass(frozen=True)
class EnergyTerm:
    """One disjoint AC-boundary cumulative energy channel contributing +1 or -1 to household demand.

    Use grid import/export plus AC supply/charging channels, or a direct household
    meter. A hybrid inverter's net AC supply/absorption can replace separate PV and
    battery channels. Raw DC counters must not be mixed into this balance.
    """

    entity_id: str
    sign: int

    def __post_init__(self):
        """Reject non-sensor identities and arbitrary scaling disguised as a sign."""
        if (
            not isinstance(self.entity_id, str)
            or len(self.entity_id) > MAX_ENTITY_ID_LENGTH
            or not re.fullmatch(r"sensor\.[a-z0-9_]+", self.entity_id)
        ):
            raise ValueError("Energy history requires a sensor")
        if type(self.sign) is not int or self.sign not in (-1, 1):
            raise ValueError("Energy balance sign must be +1 or -1")


@dataclass(frozen=True)
class ExcludedLoad:
    """Measured AC consumption of one independently planned device, subtracted before learning."""

    device_id: str
    entity_id: str

    def __post_init__(self):
        """Require a stable device identity and its own measured energy channel."""
        if (
            not isinstance(self.device_id, str)
            or not 1 <= len(self.device_id) <= MAX_DEVICE_ID_LENGTH
        ):
            raise ValueError("Invalid separately planned device")
        EnergyTerm(self.entity_id, -1)


@dataclass(frozen=True)
class DemandConfiguration:
    """Explicit household measurement boundary and minimal startup estimate, owned by the app."""

    timezone: str
    annual_kwh: float
    terms: tuple[EnergyTerm, ...]
    exclusions: tuple[ExcludedLoad, ...] = ()

    def __post_init__(self):
        """Require unique disjoint channels. Absent devices are omitted, not assumed measured zero."""
        ZoneInfo(self.timezone)
        quantity(self.annual_kwh)
        object.__setattr__(self, "annual_kwh", float(self.annual_kwh))
        if (
            not isinstance(self.terms, tuple)
            or not 0 <= len(self.terms) <= MAX_ENERGY_CHANNELS
            or any(not isinstance(energy_term, EnergyTerm) for energy_term in self.terms)
        ):
            raise ValueError("Expected up to 16 AC energy terms")
        if (
            not isinstance(self.exclusions, tuple)
            or len(self.exclusions) > MAX_ENERGY_CHANNELS
            or any(not isinstance(excluded_load, ExcludedLoad) for excluded_load in self.exclusions)
        ):
            raise ValueError("Expected bounded separately planned loads")
        if self.terms and not any(term.sign == 1 for term in self.terms):
            raise ValueError("An incoming energy channel is required")
        if not self.terms and self.exclusions:
            raise ValueError("Measured exclusions require a household energy boundary")
        entities = [term.entity_id for term in self.terms + self.exclusions]
        if len(entities) != len(set(entities)) or len(
            {excluded_load.device_id for excluded_load in self.exclusions}
        ) != len(self.exclusions):
            raise ValueError("Duplicated or overlapping demand channels")

    @property
    def version(self):
        """Bind profiles, exclusions and trained models to the exact measurement definition."""
        return digest(asdict(self))

    @property
    def entities(self):
        """Return the required history identities without provider-specific metadata."""
        return tuple(term.entity_id for term in self.terms + self.exclusions)


@dataclass(frozen=True)
class DemandHour:
    """Measured total and residual household kWh for one completed UTC hour."""

    start: datetime
    total_kwh: float
    residual_kwh: float

    def __post_init__(self):
        """Keep missing hours outside the sample set and reject impossible residual energy."""
        object.__setattr__(self, "start", aware(self.start))
        if self.start.minute or self.start.second or self.start.microsecond:
            raise ValueError("Expected a UTC hour")
        quantity(self.total_kwh)
        quantity(self.residual_kwh)
        if self.residual_kwh > self.total_kwh:
            raise ValueError("Residual demand exceeds total household demand")


@dataclass(frozen=True)
class TemperatureHour:
    """Hourly Celsius observation or forecast with explicit knowledge time."""

    start: datetime
    celsius: float
    known_at: datetime

    def __post_init__(self):
        """Distinguish weather realization from a forecast available at a historical decision."""
        object.__setattr__(self, "start", aware(self.start))
        object.__setattr__(self, "known_at", aware(self.known_at))
        if self.start.minute or self.start.second or self.start.microsecond:
            raise ValueError("Weather requires UTC hours")
        if (
            type(self.celsius) not in (int, float)
            or not isfinite(self.celsius)
            or not -MIN_TEMPERATURE_MAGNITUDE_CELSIUS <= self.celsius <= MAX_TEMPERATURE_CELSIUS
        ):
            raise ValueError("Invalid temperature")


@dataclass(frozen=True)
class ForecastStep:
    """Expected residual energy on one planning interval. Finer values are estimates, not measurements."""

    start: datetime
    duration_h: float
    kwh: float

    def __post_init__(self):
        """Accept quarter multiples through one coarse hour, including clipped final blocks."""
        object.__setattr__(self, "start", aware(self.start))
        if (
            type(self.duration_h) not in (int, float)
            or not 0 < self.duration_h * MINUTES_PER_HOUR <= COARSE_INTERVAL_MINUTES
            or self.duration_h * MINUTES_PER_HOUR % FINE_INTERVAL_MINUTES != 0
        ):
            raise ValueError("Unsupported demand resolution")
        quantity(self.kwh)


@dataclass(frozen=True)
class DemandForecast:
    """Versioned prediction evidence separated from measured load and future device schedules."""

    configuration_version: str
    training_version: str
    issued_at: datetime
    model: str
    steps: tuple[ForecastStep, ...]
    excluded_devices: tuple[str, ...]
    weather_version: str = "none"
    scope: str = "residual_household"
    quality: str = "estimated"

    @property
    def version(self):
        """Identify the exact prediction used by a plan, including its training and issue time."""
        return digest(asdict(self))


def horizon(now):
    """Build the shared UTC planning grid from the central horizon and resolution policy."""
    return planning_grid(aware(now))
