"""Provider-neutral hour values and canonical weather-evidence identities."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite

MIN_WEATHER_TEMPERATURE_CELSIUS = -90
MAX_WEATHER_TEMPERATURE_CELSIUS = 65
MAX_WIND_SPEED_M_S = 120
MAX_IRRADIANCE_W_M2 = 2000


def aware(value):
    """Require an aware weather timestamp and normalize it to UTC."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Weather timestamps require a timezone")
    return value.astimezone(UTC)


def version(value):
    """Hash the original normalized weather evidence."""
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()


def validate_quantity(value, lower, upper):
    """Reject nonnumeric and out-of-range provider measurements."""
    if type(value) not in (int, float) or not lower <= value <= upper or not isfinite(value):
        raise ValueError("Invalid weather quantity")


@dataclass(frozen=True)
class WeatherHour:
    """Mean weather for [start,start+1h), normalized from provider end-labelled radiation."""

    start: datetime
    ghi: float
    dni: float
    dhi: float
    celsius: float
    wind_ms: float
    received_at: datetime

    def __post_init__(self):
        """Reject incomplete/invalid hours and retain when evidence actually became available."""
        object.__setattr__(self, "start", aware(self.start))
        object.__setattr__(self, "received_at", aware(self.received_at))
        if self.start.minute or self.start.second or self.start.microsecond:
            raise ValueError("Weather requires UTC hours")
        for value in (self.ghi, self.dni, self.dhi):
            validate_quantity(value, 0, MAX_IRRADIANCE_W_M2)
        validate_quantity(
            self.celsius, MIN_WEATHER_TEMPERATURE_CELSIUS, MAX_WEATHER_TEMPERATURE_CELSIUS
        )
        validate_quantity(self.wind_ms, 0, MAX_WIND_SPEED_M_S)


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
            or not MIN_WEATHER_TEMPERATURE_CELSIUS
            <= self.celsius
            <= MAX_WEATHER_TEMPERATURE_CELSIUS
        ):
            raise ValueError("Invalid temperature")
