"""Validate thermal applications and derive one forecast membership per physical Energy meter."""

from bisect import bisect_left, bisect_right
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from uuid import UUID

from open_energy_planner.appliances.sources import (
    replacement_selection,
    validate_membership,
)
from open_energy_planner.appliances.thermal.model import number
from open_energy_planner.home_assistant.names import source_name

MAXIMUM_PREHEATING_C = 3
MAXIMUM_SPACE_TEMPERATURE_C = 50
ROOM_HEATER_POWER_BOUNDS_W = (100, 12000)
MAX_APPLICATION_NAME_LENGTH = 160
METERED_HOUR = timedelta(hours=1)
UNKNOWN_MODE = "Select an application linked to this heat source"
INVALID_APPLICATION = "Invalid thermal application"
DUPLICATE_AREA = "Configure only one space heating application per Home Assistant area"
UNKNOWN_AREA = "Select an available Home Assistant area"
UNKNOWN_CLIMATE = "Select an available Home Assistant climate entity"
THERMOSTAT_REQUIRED = "Link a thermostat before allowing space heating schedules"
CONFLICTING_METER = "A thermal meter cannot also control an EV or appliance cycle"
UNKNOWN_METER = "Select consumers configured in Home Assistant Energy"
OVERLAPPING_METER = "Do not select overlapping upstream/downstream meters"
MISSING_UPSTREAM = "Upstream Energy meter is missing"


class ApplicationKind(StrEnum):
    """Distinguish area-based space heating from independently named hot water."""

    SPACE_HEATING = "space_heating"
    HOT_WATER = "hot_water"


def validate_shape(applications):
    """Check persisted application identities, local permissions and structural invariants."""
    if not isinstance(applications, list):
        raise TypeError(INVALID_APPLICATION)
    identities, areas = set(), set()
    for application in applications:
        if not isinstance(application, dict) or set(application) != {
            "id",
            "kind",
            "name",
            "area_id",
            "climate_entity",
            "maximum_preheat_c",
            "sources",
        }:
            raise ValueError(INVALID_APPLICATION)
        identity = application["id"]
        if (
            not isinstance(identity, str)
            or str(UUID(identity)) != identity
            or identity in identities
        ):
            raise ValueError(INVALID_APPLICATION)
        identities.add(identity)
        if application["kind"] not in (ApplicationKind.SPACE_HEATING, ApplicationKind.HOT_WATER):
            raise ValueError(INVALID_APPLICATION)
        if not _valid_label(application["name"]):
            raise ValueError(INVALID_APPLICATION)
        _validate_sources(application["sources"], application["kind"])
        _validate_application_binding(application, areas)


def _valid_label(value):
    """Accept nonblank text within the application label length limit."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_APPLICATION_NAME_LENGTH
        and bool(value.strip())
    )


def _validate_sources(sources, kind):
    """Check unique meters, scheduling flags and optional full-load power."""
    if not isinstance(sources, list):
        raise TypeError(INVALID_APPLICATION)
    meters = set()
    for source in sources:
        if (
            not isinstance(source, dict)
            or source.keys() - {"full_load_power_w"} != {"energy_entity", "allow_scheduling"}
            or not valid_entity(source["energy_entity"], "sensor")
            or type(source["allow_scheduling"]) is not bool
            or source["energy_entity"] in meters
        ):
            raise ValueError(INVALID_APPLICATION)
        if "full_load_power_w" in source:
            if kind != ApplicationKind.SPACE_HEATING:
                raise ValueError(INVALID_APPLICATION)
            power = number(source["full_load_power_w"], *ROOM_HEATER_POWER_BOUNDS_W)
            if not power.is_integer():
                raise ValueError("Heater power requires whole watts")
        meters.add(source["energy_entity"])


def _validate_application_binding(application, areas):
    """Validate area and thermostat bindings for the application's kind."""
    area, climate, preheat = (
        application[field] for field in ("area_id", "climate_entity", "maximum_preheat_c")
    )
    if application["kind"] == ApplicationKind.SPACE_HEATING:
        if not _valid_label(area):
            raise ValueError(UNKNOWN_AREA)
        if area in areas:
            raise ValueError(DUPLICATE_AREA)
        areas.add(area)
        if climate is not None and not valid_entity(climate, "climate"):
            raise ValueError(UNKNOWN_CLIMATE)
        scheduled = any(source["allow_scheduling"] for source in application["sources"])
        if scheduled and not climate:
            raise ValueError(THERMOSTAT_REQUIRED)
        if scheduled or preheat is not None:
            number(preheat, 0, MAXIMUM_PREHEATING_C)
    elif any(value is not None for value in (area, climate, preheat)):
        raise ValueError(INVALID_APPLICATION)


def valid_entity(identity, domain):
    """Accept one bounded Home Assistant entity identity in the required domain."""
    if not isinstance(identity, str) or len(identity) > 127:
        return False
    actual, _, identifier = identity.partition(".")
    return actual == domain and identifier.isascii() and identifier.replace("_", "").isalnum()


def application_meters(configuration, kind=None):
    """Collect source identities across applications, collapsing shared meters."""
    return {
        source["energy_entity"]
        for application in configuration.get("applications", [])
        if kind is None or application["kind"] == kind
        for source in application["sources"]
    }


def validate_choices(configuration, previous, catalogue):
    """Validate new bindings while allowing retained HA sources to be temporarily unavailable."""
    areas = {row["id"] for row in catalogue.get("areas", [])}
    climates = {
        row["entity_id"]
        for row in catalogue.get("entities", [])
        if row["entity_id"].startswith("climate.")
    }
    old = {row["id"]: row for row in previous.get("applications", [])}
    for application in configuration.get("applications", []):
        saved = old.get(application["id"], {})
        area, climate = application["area_id"], application["climate_entity"]
        if area is not None and area != saved.get("area_id") and area not in areas:
            raise ValueError(UNKNOWN_AREA)
        if (
            climate is not None
            and climate != saved.get("climate_entity")
            and climate not in climates
        ):
            raise ValueError(UNKNOWN_CLIMATE)


def forecast_selection(configuration, previous, selected, options, occupied):
    """Reuse the ordinary individual forecast and preserve the household measurement boundary."""
    meters = application_meters(configuration)
    if meters & occupied:
        raise ValueError(CONFLICTING_METER)
    stores = configuration["stores"]
    # Unlinking a general heat source leaves it available as an ordinary forecast.
    # Removing a boiler retains that behavior only when another application uses its meter.
    requested = [*stores, *({"energy_entity": meter} for meter in meters)]
    detached = application_meters(previous) - {row["energy_entity"] for row in requested}
    missing_detached = detached - {row["entity_id"] for row in options}
    selected = [row for row in selected if row["entity_id"] not in missing_detached]
    selection = replacement_selection(selected, previous["stores"], requested)
    weather_meters = application_meters(configuration, ApplicationKind.SPACE_HEATING)
    selection = [
        {**row, "weather_sensitive": True} if row["entity_id"] in weather_meters else row
        for row in selection
    ]
    validate_membership(selection, options)
    return selection


def application_documents(configuration, catalogue):
    """Show the latest area and source names while retaining unavailable saved bindings."""
    metadata = {row["entity_id"]: row for row in catalogue.get("entities", [])}
    areas = {row["id"]: row["name"] for row in catalogue.get("areas", [])}
    result = []
    for application in configuration.get("applications", []):
        result.append(
            {
                **application,
                "name": areas.get(application["area_id"], application["name"]),
                "sources": [
                    {
                        **source,
                        "name": source_name(
                            metadata.get(source["energy_entity"], {}), source["energy_entity"]
                        ),
                    }
                    for source in application["sources"]
                ],
            }
        )
    return result


def shared_sources(applications):
    """Project each physical meter used in multiple applications with its permitted modes."""
    by_meter = {}
    for application in applications:
        for source in application["sources"]:
            meter = source["energy_entity"]
            row = by_meter.setdefault(
                meter, {"meter": meter, "name": source["name"], "options": []}
            )
            row["options"].append({"id": application["id"], "name": application["name"]})
    return {meter: row for meter, row in by_meter.items() if len(row["options"]) > 1}


def _ordered_mode_reports(samples):
    """Sort valid timezone-aware mode reports by time, skipping malformed reports."""
    reports = []
    for sample in samples:
        try:
            reported = datetime.fromisoformat(sample["observed_at"])
            if reported.utcoffset() is not None:
                reports.append((reported, sample["application_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    reports.sort(key=lambda report: report[0])
    return reports


def attribute_history(history, samples, linked_ids, start, validity, *, limit_to_retained=False):
    """Allocate a complete metered hour only when fresh reports agree throughout it."""
    reports = _ordered_mode_reports(samples)
    times = [reported for reported, _application in reports]
    totals = {identity: 0.0 for identity in linked_ids}
    unknown = 0.0
    retained_from = None
    if limit_to_retained and times and times[0] > start:
        oldest = times[0].astimezone(UTC)
        start = oldest.replace(minute=0, second=0, microsecond=0)
        if start < oldest:
            start += METERED_HOUR
        retained_from = start.isoformat()
    for before, after in pairwise(history.hours):
        if before.start + METERED_HOUR != after.start or after.start < start:
            continue
        # HA's hourly cumulative sum is the endpoint of the labelled hour.
        usage = after.total_kwh - before.total_kwh
        if usage < 0:
            continue
        assigned = _complete_hour_mode(reports, times, totals, after.start, validity)
        if assigned is None:
            unknown += usage
        else:
            totals[assigned] += usage
    result = {"applications_kwh": totals, "unknown_kwh": unknown}
    if retained_from is not None:
        result["retained_from"] = retained_from
    return result


def _complete_hour_mode(reports, times, linked_ids, beginning, validity):
    """Find the one linked mode that continuously covers a metered hour."""
    ending = beginning + METERED_HOUR
    index = bisect_right(times, beginning) - 1
    if index < 0:
        return None
    observation, mode = reports[index]
    if mode not in linked_ids or observation + validity < beginning:
        return None
    for reported, observed_mode in reports[index + 1 : bisect_left(times, ending)]:
        if reported > observation + validity or observed_mode != mode:
            return None
        observation = reported
    return mode if observation + validity >= ending else None
