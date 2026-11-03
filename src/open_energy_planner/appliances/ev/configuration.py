"""Bounded vehicle/charge-point settings, using HA identities rather than vendor APIs."""

from datetime import time
from uuid import UUID

from open_energy_planner.appliances.ev.model import (
    MAX_CHARGE_POINTS,
    MAX_EV_CAPACITY_KWH,
    MAX_EV_POWER_W,
    MAX_VEHICLES,
    number,
)
from open_energy_planner.core.units import FULL_SOC_PERCENT


def identity(value):
    """Require an opaque UUID, separating durable OEP identities from editable HA names."""
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Invalid EV configuration identity")
    return value


def entity(value, domain):
    """Reject arbitrary provider identifiers and transport paths at the configuration boundary."""
    if (
        not isinstance(value, str)
        or len(value) > 127
        or not value.startswith(domain + ".")
        or not value.split(".", 1)[1].replace("_", "").isalnum()
        or not value.isascii()
    ):
        raise ValueError("Invalid Home Assistant entity")
    return value


def configuration(body, catalogue, consumers, appliances):
    """Validate one complete replacement against current HA metadata and Energy ownership.

    Labels are supplied by HA, not trusted user text. A charge point must own a
    disjoint Energy consumer. Unknown or overlapping meters cannot be shifted.
    """
    if not isinstance(body, dict) or set(body) != {"vehicles", "points"}:
        raise ValueError("Specify vehicles and charge points")
    metadata = {row["entity_id"]: row for row in catalogue["entities"]}
    if (
        not isinstance(body["vehicles"], list)
        or not isinstance(body["points"], list)
        or len(body["vehicles"]) > MAX_VEHICLES
        or len(body["points"]) > MAX_CHARGE_POINTS
    ):
        raise ValueError("Too many vehicles or charge points")
    vehicles, points = [], []
    for row in body["vehicles"]:
        if not isinstance(row, dict) or set(row) - {"soc_timestamp_attribute"} != {
            "id",
            "soc_entity",
            "capacity_kwh",
            "maximum_w",
            "target_soc",
            "departure_time",
        }:
            raise ValueError("Invalid vehicle fields")
        identity(row["id"])
        source = metadata.get(entity(row["soc_entity"], "sensor"), {})
        if source.get("unit") != "%" or source.get("device_class") != "battery":
            raise ValueError("Select the vehicle's battery percentage sensor")
        number(row["capacity_kwh"], 0.1, MAX_EV_CAPACITY_KWH)
        number(row["maximum_w"], 1, MAX_EV_POWER_W)
        number(row["target_soc"], 0, FULL_SOC_PERCENT)
        departure = time.fromisoformat(row["departure_time"])
        if departure.tzinfo or departure.second or departure.microsecond:
            raise ValueError("Use a daily local departure time with minute precision")
        if round(row["maximum_w"]) != row["maximum_w"]:
            raise ValueError("Maximum charging power requires whole watts")
        attribute = row.get("soc_timestamp_attribute")
        if attribute is not None and (
            not isinstance(attribute, str)
            or not attribute
            or len(attribute) > 64
            or not attribute.replace("_", "").isalnum()
            or not attribute.isascii()
        ):
            raise ValueError("Invalid SoC sample timestamp attribute")
        vehicles.append({**row, "soc_timestamp_attribute": attribute})
    vehicle_ids = {row["id"] for row in vehicles}
    if len(vehicle_ids) != len(vehicles) or len({row["soc_entity"] for row in vehicles}) != len(
        vehicles
    ):
        raise ValueError("Vehicle identities and SoC sensors must be unique")
    for row in body["points"]:
        if not isinstance(row, dict) or set(row) != {
            "id",
            "energy_entity",
            "power_entity",
            "connected_entity",
            "minimum_w",
            "maximum_w",
            "control",
            "vehicle_ids",
        }:
            raise ValueError("Invalid charge-point fields")
        identity(row["id"])
        entity(row["energy_entity"], "sensor")
        power = metadata.get(entity(row["power_entity"], "sensor"), {})
        if power.get("device_class") != "power" or power.get("unit") not in ("W", "kW"):
            raise ValueError("Select an AC charging power sensor")
        if entity(row["connected_entity"], "binary_sensor") not in metadata:
            raise ValueError("Select the charge point's vehicle-connected binary sensor")
        if row["control"] not in ("read_only", "on_off", "variable"):
            raise ValueError("Invalid charge-point control mode")
        number(row["maximum_w"], 1, MAX_EV_POWER_W)
        number(row["minimum_w"], 0, row["maximum_w"])
        if any(round(row[key]) != row[key] for key in ("minimum_w", "maximum_w")):
            raise ValueError("Charge-point limits require whole watts")
        allowed = row["vehicle_ids"]
        if (
            not isinstance(allowed, list)
            or not allowed
            or len(set(allowed)) != len(allowed)
            or not set(allowed) <= vehicle_ids
        ):
            raise ValueError("Select the vehicles allowed at this charge point")
        if appliances.state["devices"].get(row["energy_entity"], {}).get("shiftable"):
            raise ValueError("A charge point cannot also be a shiftable cycle appliance")
        points.append(dict(row))
    if len({row["id"] for row in points}) != len(points) or len(
        {row["energy_entity"] for row in points}
    ) != len(points):
        raise ValueError("Charge-point identities and Energy meters must be unique")
    if vehicle_ids & {row["id"] for row in points}:
        raise ValueError("Vehicles and charge points require distinct identities")
    if any(
        len({row[field] for row in points}) != len(points)
        for field in ("power_entity", "connected_entity")
    ):
        raise ValueError("Each charge point requires its own power and connected source")
    from open_energy_planner.appliances.forecasting import parse_selection
    from open_energy_planner.appliances.sources import validate_membership

    selected = {row["entity_id"]: row for row in consumers.selected}
    for point in points:
        selected.setdefault(
            point["energy_entity"],
            {"entity_id": point["energy_entity"], "weather_sensitive": False},
        )
    selected = parse_selection(list(selected.values()))
    validate_membership(selected, consumers.options)
    return {"vehicles": vehicles, "points": points}, selected
