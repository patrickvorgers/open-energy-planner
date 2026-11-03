"""Authenticated HA bootstrap command. No app calculations or direct database access."""

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import websocket_api
from homeassistant.core import callback

from ..const import DOMAIN


@callback
def register(hass):
    """Register once when the integration is loaded, including before its first config entry."""
    if hass.data.get(DOMAIN + "_supervisor_command"):
        return
    hass.data[DOMAIN + "_supervisor_command"] = True
    websocket_api.async_register_command(hass, supervisor_link)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "open_energy_planner/supervisor_link",
        vol.Required("host"): str,
        vol.Required("port"): int,
        vol.Required("instance_id"): str,
        vol.Required("fingerprint"): str,
        vol.Required("secret"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def supervisor_link(hass, connection, message):
    """Accept only an authenticated administrator channel, not unauthenticated discovery metadata."""
    for flow in hass.config_entries.flow.async_progress():
        if flow["handler"] == DOMAIN and flow["context"].get("unique_id") == message["instance_id"]:
            hass.config_entries.flow.async_abort(flow["flow_id"])
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY}, data=message
    )
    if result["type"] == "abort" and result.get("reason") != "already_configured":
        connection.send_error(message["id"], "link_failed", result.get("reason", "pairing_failed"))
        return
    connection.send_result(message["id"], {"status": result["type"]})
