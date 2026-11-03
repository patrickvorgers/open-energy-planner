"""Expose simulator hooks while keeping native arithmetic independent of Home Assistant imports."""

from .calculation.energy import hour_energy as hour_energy
from .calculation.energy import quarter_energy as quarter_energy
from .policy import DOMAIN as DOMAIN
from .policy import ENERGY as ENERGY


async def async_setup(hass, config):
    """Delegate YAML setup to HA only when the framework invokes the integration hook."""
    from .runtime import async_setup as setup

    return await setup(hass, config)


async def async_setup_entry(hass, entry):
    """Start the real HA fixture adapter with its original entry-owned state and cleanup."""
    from .runtime import async_setup_entry as setup_entry

    return await setup_entry(hass, entry)


async def async_remove_entry(hass, entry):
    """Preserve the explicit removal hook and its existing retained-data behavior."""
    from .runtime import async_remove_entry as remove_entry

    return await remove_entry(hass, entry)
