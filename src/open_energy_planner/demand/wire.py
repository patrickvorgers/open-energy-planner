"""Bounded demand configuration/forecast transport with no executable model serialization."""

from dataclasses import asdict

from open_energy_planner.demand.calculation.model import (
    DemandConfiguration,
    EnergyTerm,
    ExcludedLoad,
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


def forecast_document(forecast):
    """Retain explicit units, interval times, model, quality and exclusion scope for each forecast."""
    if forecast is None:
        return None
    return {
        **asdict(forecast),
        "version": forecast.version,
        "issued_at": forecast.issued_at.isoformat(),
        "unit": "kWh",
        "steps": [
            {"start": step.start.isoformat(), "duration_h": step.duration_h, "kwh": step.kwh}
            for step in forecast.steps
        ],
    }
