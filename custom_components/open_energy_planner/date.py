"""Native Home Assistant date input on Energy-owned shiftable devices."""

from .appliance_controls import ApplianceDate
from .appliances import register_entities


async def async_setup_entry(hass, entry, async_add_entities):
    """Follow app-owned capabilities on the existing virtual device without helpers."""
    register_entities(hass, entry, async_add_entities, keys=("finish_date",), factory=ApplianceDate)
    from .ev import register_entities as register_ev
    from .ev_controls import EVDepartureDate

    register_ev(hass, entry, async_add_entities, (("vehicles", "departure_date", EVDepartureDate),))
