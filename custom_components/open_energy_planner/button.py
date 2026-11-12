"""Native Home Assistant button controls on Energy-owned shiftable devices."""

from .appliance_controls import ApplianceCancel, ApplianceReady
from .appliances import register_entities


async def async_setup_entry(hass, entry, async_add_entities):
    """Follow app-owned shift permissions and remove controls when the capability is withdrawn."""
    register_entities(
        hass,
        entry,
        async_add_entities,
        keys=("ready_for_planning", "cancel_planning"),
        factory=make_button,
    )
    from .ev import register_entities as register_ev
    from .ev_controls import EVRequestAction

    register_ev(
        hass,
        entry,
        async_add_entities,
        tuple(
            ("vehicles", key, EVRequestAction) for key in ("schedule_charging", "cancel_charging")
        ),
    )


def make_button(coordinator, entity, name, key):
    """Create readiness or cancellation on the same Energy-owned virtual device."""
    return (ApplianceReady if key == "ready_for_planning" else ApplianceCancel)(
        coordinator, entity, name, key
    )
