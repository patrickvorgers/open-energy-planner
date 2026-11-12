"""Native Home Assistant time input on Energy-owned shiftable devices."""

from .appliance_controls import ApplianceTime
from .appliances import register_entities


async def async_setup_entry(hass, entry, async_add_entities):
    """Follow app-owned capabilities on the existing virtual device without helpers."""
    register_entities(hass, entry, async_add_entities, keys=("finish_time",), factory=ApplianceTime)
    from .ev import register_entities as register_ev
    from .ev_controls import EVDepartureTime

    register_ev(
        hass,
        entry,
        async_add_entities,
        (("vehicles", "departure_time", EVDepartureTime),),
    )
