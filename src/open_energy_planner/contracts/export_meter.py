"""Normalize all configured export counters into one financially consistent meter view."""

import hashlib
from dataclasses import replace
from math import fsum

from open_energy_planner.history.model import COMPONENT_METER_TOLERANCE_KWH
from open_energy_planner.home_assistant.observations import capture_deadline


def export_sources(configuration):
    """Select required kWh roles deterministically. No import or battery meter may enter the export sum."""
    return tuple(
        sorted(
            (
                spec
                for spec in configuration.bindings
                if spec.key == "grid.export_energy" or spec.key.startswith("grid.export_energy.")
            ),
            key=lambda spec: spec.entity_id,
        )
    )


def export_identity(entities):
    """Keep a single physical identity unchanged. Bind aggregates to the exact ordered member set."""
    entities = sorted(entities)
    if len(entities) == 1:
        return entities[0]
    digest = hashlib.sha256("\n".join(entities).encode()).hexdigest()
    return "sensor.oep_export_" + digest


def seed_reading(seed, meters):
    """Read a seed's exact physical members in kWh and reject individual resets.

    Extra unrelated meters are ignored. A synthetic aggregate identity is never
    looked up as if it were a physical HA sensor. Missing members, changed
    aggregate membership and decreases beyond meter precision raise ValueError.
    Statistics validation issues, an unknown prefix or a missing physical
    endpoint also raise ValueError before reading any meters.
    """
    if seed.statistics and not seed.statistics.physical_settlement_ready:
        raise ValueError("Bonus seed has no validated physical settlement endpoint")
    components = dict(seed.component_meters) or {seed.entity_id: seed.meter_kwh}
    if export_identity(components) != seed.entity_id or not components.keys() <= meters.keys():
        raise ValueError("Bonus export meter membership is missing or changed")
    if any(
        meters[entity] < before - COMPONENT_METER_TOLERANCE_KWH
        for entity, before in components.items()
    ):
        raise ValueError("Export meter reset invalidates bonus seed")
    return fsum(meters[entity] for entity in components)


def export_reading(configuration, report, now):
    """Sum current captured kWh counters, retaining original component provenance for reset checks."""
    specs = export_sources(configuration)
    readings = {reading.key: reading for reading in report.readings} if report else {}
    selected = []
    for spec in specs:
        reading = readings.get(spec.key)
        if (
            not spec.required
            or spec.quantity != "energy"
            or reading is None
            or reading.unit != "kWh"
            or reading.entity_id != spec.entity_id
        ):
            raise ValueError("bonus_export_measurement_required")
        if not report.captured_at <= now < capture_deadline(spec, report):
            raise ValueError("bonus_export_measurement_expired")
        selected.append(reading)
    if not selected:
        raise ValueError("bonus_export_measurement_required")
    return replace(
        selected[0],
        entity_id=export_identity(spec.entity_id for spec in specs),
        value=sum(reading.value for reading in selected),
        # This is a current HA-counter view. Component records retain original timestamps.
        observed_at=report.captured_at,
        reported_at=report.captured_at,
        timestamp_kind="ha_capture",
    ), tuple(selected)
