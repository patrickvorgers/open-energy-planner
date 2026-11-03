"""Independent grid-only HA catalogue documents for native API and membership tests."""


def setup_metadata():
    """Supply a complete grid-only dashboard and typed sensors, without inventing historical energy."""
    return {
        "entities": [
            {
                "entity_id": "sensor.grid",
                "name": "Grid",
                "unit": "W",
                "device_id": "meter",
                "device_class": "power",
                "state_class": "measurement",
            },
            {
                "entity_id": "sensor.import",
                "name": "Import",
                "unit": "kWh",
                "device_id": "meter",
                "device_class": "energy",
                "state_class": "total_increasing",
            },
        ],
        "suggestions": {
            "terms": [{"entity_id": "sensor.import", "sign": 1}],
            "soc": [],
            "solar": False,
            "solar_sources": [],
            "grid_power": "sensor.grid",
            "grid_power_entities": ["sensor.grid"],
            "energy_status": "ready",
            "batteries": [],
            "grid_export": None,
        },
        "timezone": "Europe/Amsterdam",
        "history": [],
    }
