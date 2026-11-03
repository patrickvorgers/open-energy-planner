"""Exercise example-profile expectations early without installing or updating Supervisor."""

from copy import deepcopy

import pytest

from script.provisioning.acceptance import verify_device_examples


def expected_examples():
    """Specify independent learned/manual profile and blueprint outputs for the shipped examples."""
    devices = []
    for name, quarter_hour_count, energy_kwh in (
        ("dishwasher", 7, 0.83),
        ("washing_machine", 9, 0.92),
        ("dryer", 12, 1.4),
    ):
        manual = name == "dryer"
        devices.append(
            {
                "entity_id": "sensor.oep_test_" + name + "_energy",
                "shiftable": True,
                "profile_kind": "Manual" if manual else "Learned",
                "recent_cycles": ["cycle"] * (1 if manual else 2),
                "profile": {
                    "evidence": "manual" if manual else "synthetic",
                    "samples": 1 if manual else 2,
                    "power_w": [energy_kwh * 4000 / quarter_hour_count] * quarter_hour_count,
                },
            }
        )
    responses = {
        "consumers": {
            "selected": [
                {"entity_id": "sensor.oep_test_heat_pump_energy", "weather_sensitive": True}
            ]
        },
        "appliances": {"devices": devices},
        "ev": {"vehicles": [{}, {}], "points": [{}, {}]},
        "thermal": {"configuration": {"stores": [{}]}},
        "contracts": {
            "periods": [{"import_formula": "(market + 0.09161) * 1.21 + 0.02"}],
            "bonuses": [{"cap_kwh": 7500}],
        },
    }
    states = [
        {"entity_id": "automation." + name, "attributes": {"id": "oep_lab_" + name}, "state": "on"}
        for name in ("dishwasher", "washing_machine", "dryer", "wallbox_a", "wallbox_b", "boiler")
    ]
    return responses, states


@pytest.mark.parametrize("stale", [None, "profile_kind", "samples", "energy_kwh", "automation"])
def test_example_acceptance_detects_stale_profiles_and_missing_blueprints(stale):
    """The extracted assertion owner preserves real lifecycle expectations and rejects obsolete data."""
    responses, states = deepcopy(expected_examples())
    dishwasher = responses["appliances"]["devices"][0]
    if stale == "profile_kind":
        dishwasher["profile_kind"] = "Manual"
    elif stale == "samples":
        dishwasher["profile"]["samples"] = 1
    elif stale == "energy_kwh":
        dishwasher["profile"]["power_w"][0] += 100
    elif stale == "automation":
        states[0]["state"] = "off"
    if stale:
        with pytest.raises(AssertionError):
            verify_device_examples(responses.__getitem__, states)
    else:
        verify_device_examples(responses.__getitem__, states)
