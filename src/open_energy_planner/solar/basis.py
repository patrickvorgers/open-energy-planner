"""Solar forecast admission and immutable archived provenance."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from open_energy_planner.installation.forecast_policy import FORECAST_MAX_AGE_MINUTES
from open_energy_planner.installation.location import SiteLocation

from .calculation.model import SolarConfiguration, SolarForecast


def forecast_usable(
    configuration: SolarConfiguration,
    forecast: SolarForecast | None,
    location: SiteLocation | None,
    now: datetime,
) -> bool:
    """Admit configured PV only with matching configuration/site and a current forecast.

    The issuance boundary is inclusive and the thirty-minute expiry is exclusive.
    An explicit installation without PV needs no solar forecast.
    The installation compiler separately checks interval energy coverage.
    """
    return not configuration.sources or (
        forecast is not None
        and forecast.configuration_version == configuration.version
        and forecast.location == location
        and forecast.issued_at
        <= now
        < forecast.issued_at + timedelta(minutes=FORECAST_MAX_AGE_MINUTES)
        and (forecast.valid_until is None or now < forecast.valid_until)
    )


@dataclass(frozen=True)
class SolarEvidence:
    """Retain exact per-source solar forecast and measurement/configuration references in plans."""

    forecast_version: str
    configuration_version: str
    weather_version: str
    source_ids: tuple[str, ...]
    model: str
    quality: str
