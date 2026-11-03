"""Thin Home Assistant entry point for the synthetic chain milestone."""

from .const import PLATFORMS


async def async_setup(hass, config):
    """Register authenticated bootstrap before there is an installation config entry."""
    from .supervisor import register

    register(hass)
    return True


async def async_setup_entry(hass, entry):
    """Load integration intent, register virtual entities and start authenticated app synchronization."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .client import LocalClient
    from .coordinator import PlanCoordinator
    from .supervisor import register

    register(hass)

    client = LocalClient(
        async_get_clientsession(hass),
        entry.data["url"],
        entry.data["token"],
        entry.data.get("fingerprint"),
    )
    coordinator = PlanCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    from .appliances import register_services

    register_services(hass)
    import shutil
    from pathlib import Path

    def install_blueprint():
        """Install only the release-owned blueprint, without creating an automation or selecting hardware."""
        destination = Path(hass.config.path("blueprints", "automation", "open_energy_planner"))
        destination.mkdir(parents=True, exist_ok=True)
        for filename in ("appliance.yaml", "ev.yaml", "thermal.yaml"):
            shutil.copy2(Path(__file__).with_name("blueprints") / filename, destination / filename)

    await hass.async_add_executor_job(install_blueprint)
    await coordinator.load()
    await coordinator.sync()
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            registered.domain == "datetime"
            and registered.platform == "open_energy_planner"
            and registered.unique_id.startswith(entry.entry_id + "_consumer_")
            and registered.unique_id.endswith("_finish_before")
        ):
            # Retire the combined control. Its app-owned deadline remains available to the compact inputs.
            registry.async_remove(registered.entity_id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start()
    return True


async def async_unload_entry(hass, entry):
    """Stop coordinator timers/persist intent, then unload the entry's entity platforms."""
    await entry.runtime_data.close()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
