"""Share synthetic boiler configuration and quarter-hour evidence across browser scenarios."""

from datetime import UTC, datetime, timedelta

STORE = "00000000-0000-4000-8000-000000000101"


def document():
    """Declare one on/off boiler and independently specified quarter-hour chart evidence."""
    start = datetime.now(UTC).replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % 15)
    configuration = {
        "id": STORE,
        "energy_entity": "sensor.boiler_energy",
        "temperature_entity": "sensor.boiler_temperature",
        "power_entity": "sensor.boiler_power",
        "volume_litres": 120,
        "minimum_c": 45,
        "maximum_c": 65,
        "ambient_c": 20,
        "loss_w_per_c": 1.2,
        "heater_w": 2000,
        "draw_profile_kwh": [0] * 7 + [1] + [0] * 16,
        "enabled": True,
    }
    return {
        "configuration": {"stores": [configuration]},
        "configuration_version": "a" * 64,
        "revision": "b" * 64,
        "timezone": "Europe/Amsterdam",
        "issue": None,
        "catalogue": {
            "entities": [
                {
                    "entity_id": "sensor.boiler_temperature",
                    "name": "Temperature",
                    "device_name": "Utility boiler",
                    "device_class": "temperature",
                    "unit": "°C",
                },
                {
                    "entity_id": "sensor.unsupported_kelvin_temperature",
                    "name": "Unsupported temperature",
                    "device_name": "Utility boiler",
                    "device_class": "temperature",
                    "unit": "K",
                },
                {
                    "entity_id": "sensor.boiler_power",
                    "name": "Heater",
                    "device_name": "Utility boiler",
                    "device_class": "power",
                    "unit": "W",
                },
                {
                    "entity_id": "water_heater.boiler",
                    "name": "Tank",
                    "device_name": "Utility boiler",
                    "device_class": "temperature",
                    "unit": "°C",
                },
            ]
        },
        "energy_consumers": [
            {"entity_id": "sensor.boiler_energy", "name": "Utility boiler", "upstream": None}
        ],
        "devices": [
            {
                **configuration,
                "name": "Utility boiler",
                "actual": {"temperature_c": 49.25, "power_w": 0, "observed_at": start.isoformat()},
                "issue": None,
                "unmet_draw_kwh": 0.35,
                "planned": [
                    {
                        "start": (start + timedelta(minutes=15 * index)).isoformat(),
                        "end": (start + timedelta(minutes=15 * (index + 1))).isoformat(),
                        "power_w": power,
                        "end_c": temperature,
                        "shortfall_kwh": 0,
                    }
                    for index, (power, temperature) in enumerate(
                        ((2000, 52.5), (0, 52.1), (2000, 55.2))
                    )
                ],
            }
        ],
    }
