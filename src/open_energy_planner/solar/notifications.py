"""Explain solar forecast availability and confirm recovery from accepted current forecasts."""

from datetime import datetime

from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.notifications.model import NoticeText, Notification

from .basis import forecast_usable
from .calculation.model import SolarConfiguration, SolarForecast


def explain_solar_problem(reason: str, detail: str | None = None) -> NoticeText | None:
    """Own the propagated solar forecast cause and describe automatic, conditional recovery."""
    if reason != "solar_forecast_unavailable":
        return None
    message = "The solar generation forecast is missing, too old or unusable. "
    if detail == "solar_weather_expired":
        message = "Weather data for the solar forecast is too old. "
    elif detail == "solar_weather_unavailable":
        message = "Usable weather data for the solar forecast is currently unavailable. "
    return NoticeText(
        "Solar forecast is unavailable",
        message + "Weather retrieval and solar forecast updates are retried automatically. "
        "This notification clears when a valid, up-to-date solar forecast is available. "
        "If it persists, review the solar forecast status.",
        "View solar forecast",
        "/quality",
    )


def solar_forecast_notifications(
    configuration: SolarConfiguration | None,
    forecast: SolarForecast | None,
    location: SiteLocation | None,
    now: datetime,
    issue: str | None = None,
) -> tuple[Notification, ...] | None:
    """Read required PV availability without refreshing weather or mutating any product state.

    No configured PV or a usable forecast confirms no solar forecast problem.
    An unknown HA location cannot confirm recovery. The planner may independently
    retain its publication recovery notice after the solar input becomes usable.
    """
    if configuration is None or not configuration.sources:
        return ()
    if location is None:
        return None
    if forecast_usable(configuration, forecast, location, now):
        return ()
    return (
        Notification(
            "planning:solar_forecast_unavailable",
            "solar_forecast_unavailable",
            explain_solar_problem("solar_forecast_unavailable", issue),
            blocks=("planning",),
            detail=issue,
        ),
    )
