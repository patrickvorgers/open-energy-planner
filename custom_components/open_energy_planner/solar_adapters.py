"""Read selected HA solar configuration through explicit, read-only provider adapters.

Adapters expose panel geometry and declared forecast configuration only. They
never forward credentials, infer physical wiring or modify the HA integration.
The registry is the sole provider-specific seam for future supported adapters.
"""

from math import isfinite

MAX_SOLAR_PLANES = 16
MAX_SOLAR_POWER_W = 10000000
MAX_SOLAR_TILT_DEGREES = 90
MAX_SOLAR_AZIMUTH_DEGREES = 360
MAX_SOLAR_PANEL_NAME_LENGTH = 200


def _quantity(value, lower, upper):
    """Reject nonnumeric and unbounded provider settings without float overflow."""
    if type(value) not in (int, float) or not lower <= value <= upper or not isfinite(value):
        raise ValueError("Invalid solar configuration quantity")
    return float(value)


def forecast_solar_configuration(entry):
    """Read every configured Forecast.Solar plane, preserving exact north-based azimuth.

    Forecast.Solar stores module ratings in watts, tilt in degrees and azimuth
    clockwise from north. Its inverter setting is a forecast-model limit, not
    evidence that multiple entries share one physical converter.
    """
    planes = entry.get_subentries_of_type("plane")
    if not 1 <= len(planes) <= MAX_SOLAR_PLANES:
        raise ValueError("Incomplete solar panel configuration")
    panels = [
        {
            "panel_id": entry.entry_id + "." + plane.subentry_id,
            "name": entry.title if len(planes) == 1 and entry.title else plane.title,
            "capacity_w": _quantity(plane.data.get("modules_power"), 1, MAX_SOLAR_POWER_W),
            "azimuth": _quantity(plane.data.get("azimuth"), 0, MAX_SOLAR_AZIMUTH_DEGREES),
            "tilt": _quantity(plane.data.get("declination"), 0, MAX_SOLAR_TILT_DEGREES),
        }
        for plane in planes
    ]
    if any(
        not isinstance(panel["name"], str) or len(panel["name"]) > MAX_SOLAR_PANEL_NAME_LENGTH
        for panel in panels
    ):
        raise ValueError("Invalid solar panel name")
    limit = entry.options.get("inverter_size")
    return {
        "adapter": "forecast_solar",
        "entry_id": entry.entry_id,
        "panels": panels,
        "inverter_limit_w": (
            None
            if limit is None or type(limit) in (int, float) and limit == 0
            else _quantity(limit, 1, MAX_SOLAR_POWER_W)
        ),
    }


CONFIGURATION_ADAPTERS = {"forecast_solar": forecast_solar_configuration}


def solar_configuration_suggestion(hass, source):
    """Normalize the selected forecasts for one Energy production meter.

    An unsupported, missing or malformed selected entry leaves the entire group
    incomplete. Importing a partial group would silently omit panel capacity.
    A supported adapter can be added without changing catalogue or browser code.
    """
    selected = source.get("config_entry_solar_forecast")
    result = {"entity_id": source["stat_energy_from"], "providers": [], "issue": None}
    if not selected:
        return {**result, "issue": "solar_configuration_not_available"}
    if (
        not isinstance(selected, list)
        or len(selected) > MAX_SOLAR_PLANES
        or any(not isinstance(entry_id, str) for entry_id in selected)
        or len(set(selected)) != len(selected)
    ):
        return {**result, "issue": "solar_configuration_incomplete"}
    providers = []
    for entry_id in selected:
        entry = hass.config_entries.async_get_entry(entry_id)
        adapter = CONFIGURATION_ADAPTERS.get(entry.domain) if entry else None
        if adapter is None:
            return {**result, "issue": "solar_configuration_unsupported"}
        try:
            providers.append(adapter(entry))
        except (ValueError, TypeError, KeyError, AttributeError):
            return {**result, "issue": "solar_configuration_incomplete"}
    if (
        sum(len(provider["panels"]) for provider in providers) > MAX_SOLAR_PLANES
        or sum(panel["capacity_w"] for provider in providers for panel in provider["panels"])
        > MAX_SOLAR_POWER_W
    ):
        return {**result, "issue": "solar_configuration_incomplete"}
    return {**result, "providers": providers}
