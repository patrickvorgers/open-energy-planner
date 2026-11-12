"""HA-only EV readings, session identity and native virtual-device reconciliation."""

from datetime import time
from math import isfinite
from uuid import uuid4

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .measurement_sources import FULL_SOC_PERCENT, HomeAssistantMeasurementSource
from .measurements import Quantity, SourceBinding
from .protocol import timestamp
from .subentries import add_grouped_entities, retire_empty_groups
from .wire_limits import MAX_API_RESPONSE_BYTES, validate_projection_size

MAX_EV_POWER_W = 100000
MAX_EV_CAPACITY_KWH = 1000
DAYS_PER_WEEK = 7


def power_reading(hass, point, now):
    """Normalize measured AC charging power through the existing adapter, without inventing missing values."""
    binding = SourceBinding("ev.power." + point["id"], point["power_entity"], Quantity.POWER)
    result = HomeAssistantMeasurementSource(hass.states).read((binding,), now)
    return (
        result.readings[0]
        if result.ready and result.readings and result.readings[0].value >= 0
        else None
    )


def soc_reading(hass, vehicle, now):
    """Reuse timestamp-aware measurement normalization, preserving original sample/report provenance."""
    binding = SourceBinding(
        "ev.soc." + vehicle["id"],
        vehicle["soc_entity"],
        Quantity.SOC,
        timestamp_attribute=vehicle.get("soc_timestamp_attribute"),
    )
    result = HomeAssistantMeasurementSource(hass.states).read((binding,), now)
    return result.readings[0] if result.ready and result.readings else None


def watch_connections(coordinator):
    """Rotate identity on every physical transition, including reconnects hidden between HTTP polls."""
    sources = tuple(
        sorted(
            (point["connected_entity"], point["id"]) for point in coordinator.ev.get("points", [])
        )
    )
    if sources == coordinator.ev_watch:
        return
    if coordinator.ev_cancel:
        coordinator.ev_cancel()
        coordinator.ev_cancel = None
    coordinator.ev_watch = sources
    if not sources:
        return
    by_entity = dict(sources)

    @callback
    def changed(event):
        """Withdraw the previous nonce immediately without a successful app connection."""
        if coordinator.closed:
            return
        point_id = by_entity[event.data["entity_id"]]
        state = event.data.get("new_state")
        connected = {"on": True, "off": False}.get(state.state) if state else None
        previous = coordinator.ev_sessions.get(point_id)
        if previous is None or previous["connected"] != connected:
            coordinator.ev_sessions[point_id] = {"session_id": str(uuid4()), "connected": connected}
            coordinator.refresh()

    coordinator.ev_cancel = async_track_state_change_event(
        coordinator.hass, list(by_entity), changed
    )


def capture(coordinator, projection, now):
    """Create a fresh nonce per physical connection and per integration boot, never guessing a car."""
    vehicles, points = [], []
    for vehicle in projection.get("vehicles", []):
        measured = soc_reading(coordinator.hass, vehicle, now)
        vehicles.append(
            {
                "id": vehicle["id"],
                "soc": measured.value if measured else None,
                "observed_at": measured.observed_at.isoformat() if measured else None,
                "reported_at": measured.reported_at.isoformat() if measured else None,
                "timestamp_kind": measured.timestamp_kind if measured else None,
            }
        )
    for point in projection.get("points", []):
        state = coordinator.hass.states.get(point["connected_entity"])
        connected = {"on": True, "off": False}.get(state.state) if state else None
        previous = coordinator.ev_sessions.get(point["id"])
        if previous is None or previous["connected"] != connected:
            previous = {"session_id": str(uuid4()), "connected": connected}
            coordinator.ev_sessions[point["id"]] = previous
        measured = power_reading(coordinator.hass, point, now)
        points.append(
            {"id": point["id"], **previous, "power_w": measured.value if measured else None}
        )
    return {
        "configuration_version": projection["configuration_version"],
        "at": now.isoformat(),
        "vehicles": vehicles,
        "points": points,
    }


def receive(value):
    """Validate bounded app projections before dynamic entities or source reads can use them."""
    from .protocol import identifier

    validate_projection_size(value, MAX_API_RESPONSE_BYTES)

    if (
        not isinstance(value, dict)
        or not isinstance(value.get("vehicles"), list)
        or not isinstance(value.get("points"), list)
    ):
        raise TypeError("Invalid EV projection")
    identifier(value.get("configuration_version"))
    identifier(value.get("revision"))
    vehicle_ids = {identifier(row.get("id")) for row in value["vehicles"] if isinstance(row, dict)}
    for kind, entity_fields in (
        ("vehicles", ("soc_entity",)),
        ("points", ("energy_entity", "power_entity", "connected_entity")),
    ):
        identities = set()
        for row in value[kind]:
            if not isinstance(row, dict):
                raise TypeError("Invalid EV device")
            identity = identifier(row.get("id"))
            if (
                identity in identities
                or not isinstance(row.get("name"), str)
                or len(row["name"]) > 160
            ):
                raise ValueError("Invalid EV identity or name")
            identities.add(identity)
            for field in entity_fields:
                source = row.get(field)
                domain = "binary_sensor." if field == "connected_entity" else "sensor."
                if (
                    not isinstance(source, str)
                    or not source.startswith(domain)
                    or len(source) > 127
                ):
                    raise ValueError("Invalid EV source entity")
            if kind == "vehicles":
                request = row.get("charging_request")
                if request is not None:
                    if (
                        not isinstance(request, dict)
                        or request.get("status") != "planned"
                        or request.get("vehicle_id") != identity
                    ):
                        raise ValueError("Invalid EV charging request")
                    identifier(request.get("id"))
                    identifier(request.get("version"))
                    if timestamp(request.get("ready_at")) >= timestamp(request.get("departure")):
                        raise ValueError("Invalid EV request window")
                    target_soc = request.get("target_soc")
                    if (
                        type(target_soc) not in (int, float)
                        or not isfinite(target_soc)
                        or not 0 <= target_soc <= FULL_SOC_PERCENT
                    ):
                        raise ValueError("Invalid EV request target")
                    weekdays = request.get("repeat_weekdays")
                    if (
                        not isinstance(weekdays, list)
                        or any(
                            type(day) is not int or not 0 <= day < DAYS_PER_WEEK for day in weekdays
                        )
                        or len(set(weekdays)) != len(weekdays)
                    ):
                        raise ValueError("Invalid EV repeat weekdays")
                timestamp(row["next_departure"])
                clock = time.fromisoformat(row["departure_time"])
                if clock.tzinfo or clock.second or clock.microsecond:
                    raise ValueError("Invalid daily EV departure clock")
                capacity = row.get("capacity_kwh")
                if (
                    type(capacity) not in (int, float)
                    or not isfinite(capacity)
                    or not 0 < capacity <= MAX_EV_CAPACITY_KWH
                ):
                    raise ValueError("Invalid usable EV capacity")
                attribute = row.get("soc_timestamp_attribute")
                if attribute is not None and (
                    not isinstance(attribute, str) or not attribute or len(attribute) > 64
                ):
                    raise ValueError("Invalid EV sample timestamp attribute")
                if (
                    type(row.get("target_soc")) not in (int, float)
                    or not isfinite(row["target_soc"])
                    or not 0 <= row["target_soc"] <= FULL_SOC_PERCENT
                ):
                    raise ValueError("Invalid EV target SoC")
            elif row.get("control") not in ("read_only", "on_off", "variable"):
                raise ValueError("Invalid EV capability")
            maximum = row.get("maximum_w")
            if (
                type(maximum) not in (int, float)
                or not isfinite(maximum)
                or not 0 < maximum <= MAX_EV_POWER_W
            ):
                raise ValueError("Invalid EV maximum power")
            if kind == "points":
                minimum = row.get("minimum_w")
                if (
                    type(minimum) not in (int, float)
                    or not isfinite(minimum)
                    or not 0 <= minimum <= maximum
                ):
                    raise ValueError("Invalid EV minimum power")
                allowed = row.get("vehicle_ids")
                if (
                    not isinstance(allowed, list)
                    or not allowed
                    or len(set(allowed)) != len(allowed)
                    or not set(allowed) <= vehicle_ids
                    or (row.get("vehicle_id") is not None and row["vehicle_id"] not in allowed)
                ):
                    raise ValueError("Invalid EV vehicle membership")
                actual = row.get("actual")
                if not isinstance(actual, dict):
                    raise TypeError("Invalid EV connection projection")
                if actual:
                    identifier(actual.get("session_id"))
                    if (
                        actual.get("connected") is not None
                        and type(actual["connected"]) is not bool
                    ):
                        raise ValueError("Invalid EV connection state")
    if vehicle_ids & {point["id"] for point in value["points"]} or any(
        len({point[field] for point in value["points"]}) != len(value["points"])
        for field in ("power_entity", "connected_entity", "energy_entity")
    ):
        raise ValueError("Ambiguous EV source or virtual device identities")
    return value


def command(coordinator, point):
    """Gate a current plan by the same car/session and current local SoC/connection, including outages."""
    projection = coordinator.projection
    local = coordinator.ev_sessions.get(point["id"], {})
    state = coordinator.hass.states.get(point["connected_entity"])
    if (
        not projection.command
        or state is None
        or state.state != "on"
        or local.get("connected") is not True
    ):
        return None
    target = next(
        (
            row
            for row in projection.command.devices
            if row.kind == "ev" and row.device_id == point["id"]
        ),
        None,
    )
    if (
        not target
        or target.session_id != local.get("session_id")
        or target.vehicle_id != point.get("vehicle_id")
    ):
        return None
    vehicle = next(
        (row for row in coordinator.ev.get("vehicles", []) if row["id"] == target.vehicle_id), None
    )
    if not vehicle:
        return None
    request = vehicle.get("charging_request")
    now = dt_util.utcnow()
    document = getattr(getattr(coordinator, "schedule", None), "document", None)
    if (
        not request
        or request.get("status") != "planned"
        or not timestamp(request["ready_at"]) <= now < timestamp(request["departure"])
        or not document
        or document.get("ev_context", {}).get("revision") != coordinator.ev.get("revision")
    ):
        return None
    measured = soc_reading(coordinator.hass, vehicle, dt_util.utcnow())
    if measured is None or measured.value >= request["target_soc"]:
        return None
    if point["control"] == "read_only" or target.power_w > min(
        point["maximum_w"], vehicle["maximum_w"]
    ):
        return None
    return target


def register_entities(hass, entry, add, definitions):
    """Reconcile the native entities for this platform, preserving stable device/registry identities."""
    coordinator = entry.runtime_data
    entities, removing = {}, set()

    async def remove(key, entity):
        """Remove withdrawn entities before allowing a replacement with the same stable ID."""
        registry = er.async_get(hass)
        registered = registry.async_get(entity.entity_id)
        device_id = registered.device_id if registered else None
        await entity.async_remove()
        if registry.async_get(entity.entity_id):
            registry.async_remove(entity.entity_id)
        if device_id and not er.async_entries_for_device(registry, device_id):
            devices = dr.async_get(hass)
            if devices.async_get(device_id):
                devices.async_remove_device(device_id)
        entities.pop(key, None)
        removing.discard(key)
        update()
        retire_empty_groups(coordinator)

    @callback
    def update():
        """Reflect app configuration only. No physical HA devices are created or modified."""
        desired = {
            (kind, row["id"], key): (row, factory)
            for kind, key, factory in definitions
            for row in coordinator.ev.get(kind, [])
        }
        additions = []
        for key, (row, factory) in desired.items():
            device = dr.async_get(hass).async_get_device(
                identifiers={(DOMAIN, coordinator.entry.entry_id + "_ev_" + row["id"])}
            )
            if device and device.name != row["name"]:
                dr.async_get(hass).async_update_device(device.id, name=row["name"])
            if key not in entities and key not in removing:
                entities[key] = factory(coordinator, key[0], row, key[2])
                additions.append(entities[key])
        if additions:
            add_grouped_entities(coordinator, add, additions)
        for key, entity in list(entities.items()):
            if key not in desired and key not in removing:
                removing.add(key)
                entry.async_create_task(hass, remove(key, entity), "Remove EV entity")

    entry.async_on_unload(coordinator.subscribe(update))
    update()
