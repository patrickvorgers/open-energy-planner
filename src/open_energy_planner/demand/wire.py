"""Bounded demand configuration/forecast transport with no executable model serialization."""

from dataclasses import asdict
from datetime import timedelta

from open_energy_planner.demand.calculation.model import (
    DemandConfiguration,
    EnergyTerm,
    ExcludedLoad,
    ForecastStep,
    horizon,
)


def parse_configuration(body):
    """Accept only a declared AC-boundary sum, measured exclusions and one annual-demand estimate."""
    if not isinstance(body, dict) or set(body) - {
        "timezone",
        "annual_kwh",
        "terms",
        "exclusions",
        "expected_version",
    }:
        raise ValueError("Unsupported demand configuration")
    if not isinstance(body.get("terms"), list) or not isinstance(body.get("exclusions", []), list):
        raise TypeError("Invalid demand channel list")
    terms, exclusions = [], []
    for raw in body["terms"]:
        if not isinstance(raw, dict) or set(raw) != {"entity_id", "sign"}:
            raise ValueError("Invalid AC energy term")
        terms.append(EnergyTerm(**raw))
    for raw in body.get("exclusions", []):
        if not isinstance(raw, dict) or set(raw) != {"device_id", "entity_id"}:
            raise ValueError("Invalid separately planned consumer")
        exclusions.append(ExcludedLoad(**raw))
    return DemandConfiguration(
        body["timezone"], body["annual_kwh"], tuple(terms), tuple(exclusions)
    )


def configuration_document(configuration):
    """Expose app-owned demand settings with compare-and-set identity."""
    return {**asdict(configuration), "version": configuration.version} if configuration else None


def forecast_window(steps, now):
    """Integrate the stored issue onto the rolling planning grid within its coverage."""
    from .calculation.composition import integrals

    if not steps:
        return ()
    end = steps[-1].start + timedelta(hours=steps[-1].duration_h)
    grid = [
        (start, duration)
        for start, duration in horizon(now)
        if start >= steps[0].start and start + timedelta(hours=duration) <= end
    ]
    values = integrals(
        steps, ((start, start + timedelta(hours=duration)) for start, duration in grid)
    )
    return tuple(
        ForecastStep(start, duration, value)
        for (start, duration), value in zip(grid, values, strict=True)
    )


def forecast_document(forecast, *, now=None):
    """Retain explicit units, interval times, model, quality and exclusion scope for each forecast."""
    if forecast is None:
        return None
    body = asdict(forecast)
    del body["checked_at"]
    del body["weather_valid_until"]
    steps = forecast.steps if now is None else forecast_window(forecast.steps, now)
    return {
        **body,
        "version": forecast.version,
        "issued_at": forecast.issued_at.isoformat(),
        "unit": "kWh",
        "steps": [
            {"start": step.start.isoformat(), "duration_h": step.duration_h, "kwh": step.kwh}
            for step in steps
        ],
    }
