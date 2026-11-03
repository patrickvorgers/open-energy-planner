"""Native EV target percentage controls without additional helpers."""

from .ev import register_entities
from .ev_controls import EVTarget


async def async_setup_entry(hass, entry, async_add_entities):
    """Expose one persisted target percentage per configured vehicle."""
    register_entities(hass, entry, async_add_entities, (("vehicles", "target_soc", EVTarget),))
    from .thermal import register_entities as register_thermal

    register_thermal(entry.runtime_data, async_add_entities, "number")
