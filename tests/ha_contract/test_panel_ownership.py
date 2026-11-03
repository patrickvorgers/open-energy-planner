"""Retain accepted HA panel ownership when discovery cannot confirm an unlink."""

import pytest

from open_energy_planner.home_assistant.solar_panels import follow_panels, panel_settings
from open_energy_planner.solar.calculation.model import SolarImport, SolarPanel, SolarSource


@pytest.mark.parametrize("discovery_status", ["stale", "missing", "not_ready", "omitted"])
def test_unknown_discovery_cannot_release_accepted_panel_ownership(discovery_status):
    """Only a fresh complete explicit unlink can release geometry retained in an accepted HA snapshot."""
    plane = SolarPanel("garden", 2400, 110, 20)
    source = SolarSource(
        "garden",
        "sensor.garden",
        "converter",
        2400,
        "east",
        panels=(plane,),
        ha_imports=(SolarImport("forecast_solar", "garden", (plane,)),),
    )
    catalogue = {
        "stale": discovery_status == "stale",
        "data": {
            "suggestions": {
                "energy_status": "incomplete" if discovery_status == "not_ready" else "ready",
                "solar_configurations": []
                if discovery_status == "omitted"
                else [
                    {
                        "entity_id": "sensor.garden",
                        "issue": "solar_configuration_not_available",
                        "providers": [],
                    }
                ],
            }
        },
    }
    if discovery_status == "missing":
        catalogue["data"] = None
    retained = panel_settings(catalogue, source.entity_id, source)
    assert retained.document() == {"state": "unavailable", "managed": True}
    assert follow_panels(source, retained) == source
