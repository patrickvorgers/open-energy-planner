"""Bind forecast measurements to frozen explicit source identities."""

from open_energy_planner.demand.calculation.model import digest


def bindings(demand, solar, consumers, catalogue):
    """Bind metering to its HA registry device and unit, preventing cross-device evidence reuse."""
    entities = set(demand.entities if demand else ())
    entities.update(source.entity_id for source in (solar.sources if solar else ()))
    entities.update(row["entity_id"] for row in consumers)
    metadata = {row["entity_id"]: row for row in (catalogue or {}).get("entities", [])}
    result = []
    for entity in sorted(entities):
        row = metadata.get(entity)
        if row is None:
            continue
        identity = {"entity_id": entity, "device_id": row.get("device_id", ""), "unit": row["unit"]}
        result.append({**identity, "version": digest(identity)})
    return result
