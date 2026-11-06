"""Strict local solar API serialization. No provider or native modeling objects."""

import json
from dataclasses import asdict, fields

from open_energy_planner.solar.calculation.model import (
    MAX_SOLAR_POWER_W,
    SolarConfiguration,
    SolarImport,
    SolarInverter,
    SolarPanel,
    SolarSource,
)


def parse_configuration(body):
    """Read bounded explicit sources and shared inverters, rejecting unknown configuration fields."""
    if not isinstance(body, dict) or set(body) - {
        "sources",
        "inverters",
        "expected_version",
        "version",
    }:
        raise ValueError("Invalid solar configuration")
    sources, inverters = body["sources"], body["inverters"]
    if not isinstance(sources, list) or not isinstance(inverters, list):
        raise TypeError("Expected bounded solar lists")
    return SolarConfiguration(
        tuple(parse_source(source) for source in sources),
        tuple(SolarInverter(**inverter) for inverter in inverters),
    )


def parse_import(body):
    """Decode the original credential-free adapter snapshot with exact per-plane geometry."""
    if not isinstance(body, dict):
        raise TypeError("Expected solar import configuration")
    return SolarImport(**{**body, "panels": parse_panels(body["panels"])})


def parse_panels(body):
    """Bound nested panel lists before constructing their independently validated geometry."""
    if not isinstance(body, (list, tuple)):
        raise TypeError("Expected bounded solar panels")
    return tuple(SolarPanel(**panel) for panel in body)


def parse_source(body):
    """Decode editable planes separately from their original HA import provenance."""
    if not isinstance(body, dict):
        raise TypeError("Expected solar source configuration")
    providers = body.get("ha_imports", ())
    if not isinstance(providers, (list, tuple)):
        raise TypeError("Expected bounded solar imports")
    return SolarSource(
        **{
            **body,
            "panels": parse_panels(body.get("panels", ())),
            "ha_imports": tuple(parse_import(provider) for provider in providers),
        }
    )


def validate_configuration_suggestions(body, meters):
    """Validate bounded provider-neutral imports against their actual Energy production meters."""
    if not isinstance(body, list):
        raise TypeError("Expected bounded solar configuration suggestions")
    seen = set()
    assigned = set()
    for group in body:
        if not isinstance(group, dict) or set(group) != {"entity_id", "providers", "issue"}:
            raise ValueError("Invalid solar configuration suggestion")
        entity = group["entity_id"]
        if not isinstance(entity, str) or entity not in meters or entity in seen:
            raise ValueError("Solar configuration suggestion does not match Energy")
        seen.add(entity)
        providers = group["providers"]
        if not isinstance(providers, list):
            raise TypeError("Invalid solar configuration providers")
        issue = group["issue"]
        if issue not in (
            None,
            "solar_configuration_not_available",
            "solar_configuration_incomplete",
            "solar_configuration_unsupported",
            "solar_configuration_ambiguous",
        ) or (issue is not None and providers):
            raise ValueError("Invalid solar configuration availability")
        imports = tuple(parse_import(provider) for provider in providers)
        identities = {(provider.adapter, provider.entry_id) for provider in imports}
        if identities & assigned:
            raise ValueError("Solar provider assigned to multiple meters")
        assigned.update(identities)
        if not imports and issue is None:
            raise ValueError("Solar configuration is not confirmed available")
        if len({(provider.adapter, provider.entry_id) for provider in imports}) != len(imports):
            raise ValueError("Invalid solar configuration group")
        panels = tuple(panel for provider in imports for panel in provider.panels)
        if sum(panel.capacity_w for panel in panels) > MAX_SOLAR_POWER_W:
            raise ValueError("Imported solar capacity exceeds installation bounds")
        if len({panel.panel_id for panel in panels}) != len(panels):
            raise ValueError("Duplicated imported solar plane")


def document(value):
    """Detach dataclasses into JSON-compatible evidence, retaining explicit dates and versions."""
    return json.loads(json.dumps(asdict(value), default=str, allow_nan=False))


def configuration_document(value):
    """Return an editable settings document with its compare-and-set version."""
    if value is None:
        return None
    body = document(value)
    # Preserve the content of immutable archives written before optional names existed.
    for source in body["sources"]:
        for group in (source, *source["ha_imports"]):
            for panel in group["panels"]:
                if not panel["name"]:
                    panel.pop("name")
    return {**body, "version": value.version}


def forecast_document(value, *, series=True):
    """Expose the exact forecast and provenance, including its immutable archive reference."""
    if value is None:
        return None
    if series:
        return {**document(value), "version": value.version}
    metadata = {
        field.name: getattr(value, field.name)
        for field in fields(value)
        if field.name != "energies"
    }
    metadata["location"] = document(value.location)
    return {**json.loads(json.dumps(metadata, default=str)), "version": value.version}
