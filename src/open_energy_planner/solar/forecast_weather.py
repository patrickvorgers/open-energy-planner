"""Frozen original forecast runs with separate simulation, initialization and receipt clocks."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from open_energy_planner.planning_policy import PLANNING_HORIZON_HOURS
from open_energy_planner.prices.model import aware
from open_energy_planner.solar.calculation.model import SolarWeatherHour, version

FORECAST_WEATHER_MODEL = "ecmwf_ifs"
FORECAST_WEATHER_POLICY = "ecmwf-ifs-frozen-original-runs-v1"
DEFAULT_FORECAST_ORIGIN_DELAY_HOURS = 24
MAX_FORECAST_RUN_HOURS = 120
MAX_LIVE_RUN_AGE_HOURS = 24
FORECAST_RUN_RESPONSE_HOURS = 96
SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
HOUR = timedelta(hours=1)
RUN_CYCLE_HOURS = 6


@dataclass(frozen=True, kw_only=True)
class ForecastWeatherRun:
    """Retain one exact forecast run without pretending retrieval occurred in the past.

    The default origin is an explicit simulation assumption, not a provider
    publication timestamp. Every planning hour and its final interpolation
    neighbor must be complete. Unknown actual publication remains None.
    """

    initialized_at: datetime
    received_at: datetime
    location_version: str
    hours: tuple[SolarWeatherHour, ...]
    model: str = FORECAST_WEATHER_MODEL
    forecast_origin: datetime | None = None
    available_at: datetime | None = None
    policy_version: str = FORECAST_WEATHER_POLICY

    def __post_init__(self):
        """Validate frozen model/site identity, real receipt clocks and full normalized UTC coverage."""
        initialized = aware(self.initialized_at).astimezone(UTC)
        received = aware(self.received_at).astimezone(UTC)
        origin = (
            aware(self.forecast_origin).astimezone(UTC)
            if self.forecast_origin is not None
            else initialized + timedelta(hours=DEFAULT_FORECAST_ORIGIN_DELAY_HOURS)
        )
        available = (
            aware(self.available_at).astimezone(UTC) if self.available_at is not None else None
        )
        if (
            self.model != FORECAST_WEATHER_MODEL
            or self.policy_version != FORECAST_WEATHER_POLICY
            or initialized.hour % RUN_CYCLE_HOURS
            or any(
                instant.minute or instant.second or instant.microsecond
                for instant in (initialized, origin)
            )
            or received < initialized
            or origin < initialized
            or available is not None
            and not initialized <= available <= received
            or not isinstance(self.location_version, str)
            or len(self.location_version) != 64
            or any(character not in "0123456789abcdef" for character in self.location_version)
        ):
            raise ValueError("Invalid frozen solar forecast identity")
        hours = tuple(self.hours)
        if not 1 <= len(hours) <= MAX_FORECAST_RUN_HOURS or any(
            not isinstance(row, SolarWeatherHour)
            or index > 0
            and row.start <= hours[index - 1].start
            or row.received_at != received
            for index, row in enumerate(hours)
        ):
            raise ValueError("Incomplete frozen solar forecast horizon")
        present = {row.start for row in hours}
        if any(origin + index * HOUR not in present for index in range(PLANNING_HORIZON_HOURS + 1)):
            raise ValueError("Incomplete frozen solar forecast horizon")
        object.__setattr__(self, "initialized_at", initialized)
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "forecast_origin", origin)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "hours", hours)

    @property
    def version(self):
        """Identify original values and all independent provenance clocks without mutable cache pointers."""
        return version(asdict(self))
