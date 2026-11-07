"""Atomic versioned installation settings. No copies of HA history or provider objects."""

import json

from open_energy_planner.home_assistant.observation_wire import parse_configuration
from open_energy_planner.installation.model import (
    Installation,
    InstalledBattery,
)
from open_energy_planner.solar.calculation.model import SolarInverter

ARCHIVE_HASH_LENGTH = 64


def parse(body):
    """Read only declared installation fields, rejecting unknown executable/provider configuration."""
    if not isinstance(body, dict):
        raise TypeError("Installation settings required")
    data = dict(body)
    for key in ("version", "expected_version"):
        data.pop(key, None)
    for key in ("inverters", "batteries"):
        if not isinstance(data.get(key, []), list):
            raise TypeError("Invalid installation collection")
    data["inverters"] = tuple(SolarInverter(**inverter) for inverter in data.get("inverters", []))
    data["batteries"] = tuple(
        InstalledBattery(**battery_item) for battery_item in data.get("batteries", [])
    )
    for field in ("additional_grid_power_entities", "additional_grid_export_entities"):
        if field in data:
            if not isinstance(data[field], list):
                raise ValueError("Grid sensors must be a list")
            data[field] = tuple(data[field])
    settings = Installation(**data)
    sources(settings)
    return settings


def document(settings):
    """Expose plain editable settings with an exact compare-and-set identity."""
    return {**settings.record(), "version": settings.version} if settings else None


def sources(settings):
    """Derive the required sensor roles once from physical configuration. Absent batteries require no SoC."""
    bindings = [
        {
            "key": "grid.power",
            "entity_id": settings.grid_power_entity,
            "quantity": "power",
            "multiplier": -1 if settings.grid_power_inverted else 1,
        }
    ]
    bindings.extend(
        {
            "key": f"grid.power.{index}",
            "entity_id": entity,
            "quantity": "power",
        }
        for index, entity in enumerate(settings.additional_grid_power_entities, start=1)
    )
    if settings.grid_export_entity:
        bindings.append(
            {
                "key": "grid.export_energy",
                "entity_id": settings.grid_export_entity,
                "quantity": "energy",
            }
        )
    bindings.extend(
        {"key": f"grid.export_energy.{index}", "entity_id": entity, "quantity": "energy"}
        for index, entity in enumerate(settings.additional_grid_export_entities, start=1)
    )
    for battery in settings.batteries:
        bindings.extend(
            [
                {
                    "key": f"battery.{battery.device_id}.soc",
                    "entity_id": battery.soc_entity,
                    "quantity": "soc",
                },
                {
                    "key": f"battery.{battery.device_id}.power",
                    "entity_id": battery.power_entity,
                    "quantity": "power",
                    "multiplier": -1 if battery.power_inverted else 1,
                },
            ]
        )
    return parse_configuration({"bindings": bindings}, revision=settings.version)


class InstallationStore:
    """Archive immutable definitions before updating one durable active reference."""

    def __init__(self, documents):
        """Restore checked settings. Corruption is explicit and cannot become an empty installation."""
        self.documents = documents
        self.current, self.issue = None, None
        try:
            pointer = "current.json"
            if self.documents.exists(pointer):
                ref = self.documents.read(pointer)
                if (
                    not isinstance(ref, str)
                    or len(ref) != ARCHIVE_HASH_LENGTH
                    or any(candidate not in "0123456789abcdef" for candidate in ref)
                ):
                    raise ValueError("Invalid installation reference")
                self.current = parse(self.documents.read(f"{ref}.json"))
                if self.current.version != ref:
                    raise ValueError("Installation evidence mismatch")
        except (ValueError, TypeError, KeyError, OSError):
            self.current, self.issue = None, "installation_configuration_unreadable"

    def save(self, settings):
        """Persist a whole revision without rewriting any previous plan/configuration evidence."""
        with self.documents.transaction():
            body = document(settings)
            target = f"{settings.version}.json"
            if self.documents.exists(target):
                if self.documents.read(target) != json.loads(json.dumps(body)):
                    raise ValueError("Immutable installation evidence mismatch")
            else:
                self.documents.write(target, body)
            self.documents.write("current.json", settings.version)

        self.current, self.issue = settings, None
