"""Shared Home Assistant boiler catalogue and declared thermal settings."""

DEVICE = "00000000-0000-4000-8000-000000000010"
ENERGY = "sensor.boiler_energy"


def catalogue():
    """Keep the boiler's electrical meter explicitly owned by Home Assistant Energy."""
    return {
        "entities": [
            {"entity_id": ENERGY, "device_class": "energy", "unit": "kWh", "device_name": "Boiler"},
            {
                "entity_id": "sensor.boiler_temperature",
                "device_class": "temperature",
                "unit": "°C",
            },
            {"entity_id": "sensor.boiler_power", "device_class": "power", "unit": "W"},
        ],
        "suggestions": {"consumers": [{"entity_id": ENERGY, "upstream": None}]},
    }


def settings():
    """Use declared hourly heat demand without inferring hot water from electrical input."""
    return {
        "stores": [
            {
                "id": DEVICE,
                "energy_entity": ENERGY,
                "temperature_entity": "sensor.boiler_temperature",
                "power_entity": "sensor.boiler_power",
                "volume_litres": 100,
                "minimum_c": 45,
                "maximum_c": 70,
                "ambient_c": 20,
                "loss_w_per_c": 2,
                "heater_w": 2000,
                "draw_profile_kwh": [0.2] * 24,
                "enabled": True,
            }
        ]
    }
