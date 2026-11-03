"""Current, fail-closed EV charging permission for user-owned automations."""

from .ev import register_entities
from .ev_controls import EVCharging


async def async_setup_entry(hass, entry, async_add_entities):
    """Expose permission independently of actual measured charging or physical execution."""
    register_entities(hass, entry, async_add_entities, (("points", "charging", EVCharging),))
    from .thermal import register_entities as register_thermal

    register_thermal(entry.runtime_data, async_add_entities, "binary_sensor")
