"""Explicit connected-car selection for shared charge points."""

from .ev import register_entities
from .ev_controls import EVVehicle


async def async_setup_entry(hass, entry, async_add_entities):
    """Expose the current-session car identity without guessing or creating a helper."""
    register_entities(hass, entry, async_add_entities, (("points", "vehicle", EVVehicle),))
