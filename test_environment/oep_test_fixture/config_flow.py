"""Create one laboratory-only fixture entry. No external connection or physical services."""

import voluptuous as vol
from homeassistant import config_entries

DOMAIN = "oep_test_fixture"


class FixtureFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Own the stable fixture identity needed for simulated HA device registry entries."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Load synthetic recent data on the designated VM's existing dashboard identities, without replacing sources."""
        if user_input and user_input.get("confirm_test_data"):
            await self.async_set_unique_id("oep-existing-energy-test-only")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="SIMULATION: existing Energy sources", data={"existing_energy": True}
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("confirm_test_data", default=False): bool}),
        )

    async def async_step_import(self, _data):
        """Create one YAML-imported fixture entry, or reuse the existing persistent installation."""
        await self.async_set_unique_id("oep-user-test-only")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Simulated test installation", data={})
