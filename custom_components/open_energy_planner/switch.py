"""Persisted EMS enable intent, distinct from operational status."""

from homeassistant.components.switch import SwitchEntity

from .entity import PlannerEntity


async def async_setup_entry(hass, entry, async_add_entities):
    """Register the persisted EMS enable-intent switch for this installation."""
    async_add_entities([PlannerSwitch(entry.runtime_data, "ems", "enabled", "Enabled")])
    from .thermal import register_entities

    register_entities(entry.runtime_data, async_add_entities, "switch")


class PlannerSwitch(PlannerEntity, SwitchEntity):
    """Expose desired EMS enablement independently of actual planning/availability status."""

    @property
    def is_on(self):
        """Return persisted desired enablement rather than inferring it from active commands."""
        return self.coordinator.schedule.enabled

    async def async_turn_on(self, **kwargs):
        """Explicitly resume the EMS and require a newly validated plan."""
        await self.coordinator.set_enabled(True)

    async def async_turn_off(self, **kwargs):
        """Pause virtual command publication and persist the user's disabled intent."""
        await self.coordinator.set_enabled(False)
