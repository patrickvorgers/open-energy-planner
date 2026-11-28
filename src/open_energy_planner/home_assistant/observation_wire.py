"""Strict JSON boundary for app-owned source configuration and HA reports."""

import re
from dataclasses import asdict
from datetime import UTC, datetime
from math import isfinite
from uuid import uuid4

from open_energy_planner.core.units import FULL_SOC_PERCENT
from open_energy_planner.home_assistant.observations import (
    Observation,
    ObservationIssue,
    ObservationReport,
    SourceConfiguration,
    SourceSpec,
)
from open_energy_planner.installation.location import SiteLocation

MAX_IDENTIFIER_LENGTH = 128
MAX_READING_AGE_SECONDS = 300
MAX_SOURCE_AGE_SECONDS = 3600
MAX_REPORT_TRANSPORT_AGE_SECONDS = 30

UNITS = {"power": "W", "energy": "kWh", "soc": "%"}


def identifier(value):
    """Return a nonblank bounded string identity or reject malformed input."""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError("Invalid identifier")
    return value


def number(value, *, minimum=None, maximum=None):
    """Require a finite non-boolean number within any inclusive supplied bounds."""
    try:
        valid = type(value) in (int, float) and isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Invalid finite number")
    below_minimum = minimum is not None and value < minimum
    if below_minimum or (maximum is not None and value > maximum):
        raise ValueError("Invalid finite number")
    return value


def stamp(value, now):
    """Parse an aware timestamp as UTC and reject values later than the supplied clock."""
    try:
        parsed = datetime.fromisoformat(value).astimezone(UTC)
        if datetime.fromisoformat(value).utcoffset() is None or parsed > now:
            raise ValueError
        return parsed
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Invalid observation timestamp") from None


def parse_configuration(body, *, revision=None):
    """Validate unique supported sensor roles and create an app-owned source revision.

    Grid power is required. Only power permits sign inversion. Maximum ages are
    seconds and normalized quantities are W, kWh or percent.
    """
    if not isinstance(body, dict) or not isinstance(body.get("bindings"), list):
        raise TypeError("Source bindings are required")
    if not body["bindings"]:
        raise ValueError("Select at least one source")
    specs = tuple(_parse_source_binding(raw) for raw in body["bindings"])
    _validate_source_roles(specs)
    return SourceConfiguration(revision or str(uuid4()), specs)


def _parse_source_binding(raw):
    """Validate one bound sensor's identity, direction and maximum age."""
    if not isinstance(raw, dict):
        raise TypeError("Invalid source binding")
    key, entity = identifier(raw.get("key")), identifier(raw.get("entity_id"))
    quantity = raw.get("quantity")
    if not re.fullmatch(r"sensor\.[a-z0-9_]+", entity) or quantity not in UNITS:
        raise ValueError("Select a supported sensor quantity")
    required, multiplier = raw.get("required", True), raw.get("multiplier", 1)
    age = number(
        raw.get("maximum_age_s", MAX_READING_AGE_SECONDS),
        minimum=1,
        maximum=MAX_SOURCE_AGE_SECONDS,
    )
    attribute = raw.get("timestamp_attribute")
    if type(required) is not bool or type(multiplier) is not int or multiplier not in (-1, 1):
        raise ValueError("Invalid source direction or required flag")
    if quantity != "power" and multiplier != 1:
        raise ValueError("Only power sources support direction inversion")
    if attribute is not None:
        identifier(attribute)
    return SourceSpec(key, entity, quantity, required, multiplier, age, attribute)


def _validate_source_roles(specs):
    """Require the primary grid and validate quantity and presence rules across sensors."""
    if len({spec.key for spec in specs}) != len(specs) or not any(
        source.required for source in specs
    ):
        raise ValueError("Unique source keys and at least one required source are needed")
    if not any(spec.key == "grid.power" and spec.required for spec in specs):
        raise ValueError("The grid power source is required")
    expected = {
        "grid.power": "power",
        "battery.power": "power",
        "battery.soc": "soc",
        "household.power": "power",
        "pv.power": "power",
        "grid.export_energy": "energy",
    }
    if any(spec.key in expected and spec.quantity != expected[spec.key] for spec in specs):
        raise ValueError("Source role has an incompatible quantity")
    if any(
        spec.key.startswith("grid.power.") and (spec.quantity != "power" or not spec.required)
        for spec in specs
    ):
        raise ValueError("Every additional grid power source must be required power")
    if any(
        spec.key.startswith("grid.export_energy.")
        and (spec.quantity != "energy" or not spec.required)
        for spec in specs
    ):
        raise ValueError("Every additional export meter must be required energy")


def configuration_document(configuration):
    """Serialize the app-owned revision and immutable source bindings for transport/storage."""
    return {
        "revision": configuration.revision,
        "bindings": [asdict(spec) for spec in configuration.bindings],
    }


def observation_status(configuration, report):
    """Expose report identity, capture time and source issues without fabricating measurements."""
    return {
        "config_revision": configuration.revision if configuration else None,
        "measurement_version": report.version if report else None,
        "captured_at": report.captured_at.isoformat() if report else None,
        "issues": [asdict(issue) for issue in report.issues] if report else [],
    }


def parse_report(body, configuration, now):
    """Validate complete normalized readings/issues against the current source revision.

    Every source appears exactly once with the expected entity and normalized unit.
    Capture must arrive within 30 seconds. Device/report timestamps cannot exceed it.
    Per-source freshness is checked separately during snapshot assembly.
    """
    if not isinstance(body, dict) or body.get("config_revision") != configuration.revision:
        raise ValueError("Observation configuration mismatch")
    captured = stamp(body.get("captured_at"), now)
    if (now - captured).total_seconds() >= MAX_REPORT_TRANSPORT_AGE_SECONDS:
        raise ValueError("Observation report expired")
    raw_readings, raw_issues = body.get("readings"), body.get("issues")
    if not isinstance(raw_readings, list) or not isinstance(raw_issues, list):
        raise TypeError("Readings and issues must be lists")
    specs = {spec.key: spec for spec in configuration.bindings}
    _validate_observation_membership([*raw_readings, *raw_issues], specs)
    readings = tuple(_parse_reading(raw, specs[raw["key"]], captured) for raw in raw_readings)
    issues = tuple(
        ObservationIssue(raw["key"], raw["entity_id"], identifier(raw.get("reason")))
        for raw in raw_issues
    )
    location = _parse_report_location(body.get("location"))
    return ObservationReport(
        str(uuid4()), configuration.revision, captured, readings, issues, location
    )


def _validate_observation_membership(entries, specs):
    """Check the complete report's source identities before reading any values."""
    seen = set()
    for raw in entries:
        if not isinstance(raw, dict) or not isinstance(raw.get("key"), str):
            raise TypeError("Invalid observation entry")
        spec = specs.get(raw["key"])
        if spec is None or raw.get("entity_id") != spec.entity_id or spec.key in seen:
            raise ValueError("Unexpected, duplicate or mismatched observation")
        seen.add(spec.key)
    if seen != set(specs):
        raise ValueError("Every configured source requires a reading or issue")


def _parse_reading(raw, spec, captured):
    """Validate one measurement's units, bounds and timestamp provenance."""
    value = number(raw.get("value"))
    if raw.get("unit") != UNITS[spec.quantity]:
        raise ValueError("Expected normalized observation units")
    if spec.quantity == "soc" and not 0 <= value <= FULL_SOC_PERCENT:
        raise ValueError("Invalid battery SoC")
    if spec.quantity == "energy" and value < 0:
        raise ValueError("Invalid cumulative energy")
    kind = raw.get("timestamp_kind")
    if kind not in (("device_sample",) if spec.timestamp_attribute else ("ha_report", "ha_state")):
        raise ValueError("Observation timestamp provenance mismatch")
    observed = stamp(raw.get("observed_at"), captured)
    reported = stamp(raw.get("reported_at"), captured)
    return Observation(spec.key, spec.entity_id, value, raw["unit"], observed, reported, kind)


def _parse_report_location(raw):
    """Validate the optional location's exact fields before constructing it."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"latitude", "longitude", "timezone"}:
        raise ValueError("Invalid Home Assistant location")
    return SiteLocation(**raw)
