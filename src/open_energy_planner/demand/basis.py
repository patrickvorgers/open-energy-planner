"""Immutable archived demand forecast provenance."""

from dataclasses import dataclass
from datetime import timedelta

from open_energy_planner.installation.forecast_policy import FORECAST_MAX_AGE_MINUTES


def forecast_usable(configuration, forecast, now):
    """Share the exact configuration and freshness boundary used by planning admission."""
    return bool(
        configuration
        and forecast
        and forecast.configuration_version == configuration.version
        and forecast.issued_at
        <= now
        < forecast.issued_at + timedelta(minutes=FORECAST_MAX_AGE_MINUTES)
    )


@dataclass(frozen=True)
class DemandEvidence:
    """Retain the original archived demand prediction, metering definition and model.

    The forecast version identifies the original immutable prediction archive.
    Transforming load does not change that source identity.
    """

    forecast_version: str
    configuration_version: str
    training_version: str
    model: str
    quality: str
    excluded_devices: tuple[str, ...]
