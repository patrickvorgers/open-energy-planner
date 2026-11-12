"""Forward complete device-targeted charging requests to the app-owned lifecycle."""

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN


def register_services(hass):
    """Register request creation, editing and cancellation without local planning or helpers."""
    if hass.services.has_service(DOMAIN, "ev_schedule"):
        return

    async def handle(call):
        """Resolve only a virtual OEP vehicle and forward the same atomic API used by the app."""
        device = dr.async_get(hass).async_get(call.data["device_id"])
        matches = []
        if device:
            for entry in hass.config_entries.async_entries(DOMAIN):
                coordinator = getattr(entry, "runtime_data", None)
                if coordinator is None or coordinator.closed:
                    continue
                for vehicle in coordinator.ev.get("vehicles", []):
                    if (DOMAIN, entry.entry_id + "_ev_" + vehicle["id"]) in device.identifiers:
                        matches.append((coordinator, vehicle))
        if len(matches) != 1:
            raise HomeAssistantError("Choose one virtual OEP vehicle device")
        coordinator, vehicle = matches[0]
        body = {key: value for key, value in call.data.items() if key != "device_id"}
        operation = "requests" if call.service == "ev_schedule" else "cancel"
        body["vehicle_id"] = vehicle["id"]
        try:
            accepted = await coordinator.write_ev(operation, body)
        except (ClientError, TimeoutError, ValueError, KeyError) as error:
            raise HomeAssistantError("The app could not accept this charging request") from error
        current = next(
            (row for row in accepted.get("vehicles", []) if row["id"] == vehicle["id"]), {}
        )
        return {"charging_request": current.get("charging_request")}

    shared = {vol.Required("device_id"): cv.string, vol.Required("request_id"): cv.string}
    schemas = {
        "ev_schedule": {
            **shared,
            vol.Optional("target_soc"): vol.Coerce(float),
            vol.Optional("departure"): cv.string,
            vol.Optional("repeat_weekdays"): [vol.All(vol.Coerce(int), vol.Range(min=0, max=6))],
            vol.Optional("expected_version"): cv.string,
        },
        "ev_cancel": {**shared, vol.Required("expected_version"): cv.string},
    }
    for service, fields in schemas.items():
        hass.services.async_register(
            DOMAIN,
            service,
            handle,
            schema=vol.Schema(fields),
            supports_response=SupportsResponse.OPTIONAL,
        )
