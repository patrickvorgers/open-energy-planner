"""Explicit EV identities and wire inputs without database configuration or real HA startup."""

from datetime import UTC, datetime

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)

CAR = "00000000-0000-4000-8000-000000000001"

OTHER_CAR = "00000000-0000-4000-8000-000000000002"

POINT = "00000000-0000-4000-8000-000000000003"

OTHER_POINT = "00000000-0000-4000-8000-000000000004"

SESSION = "00000000-0000-4000-8000-000000000005"


def catalogue():
    """Provide two explicitly separate cars and one Energy-owned charging consumer, without device guessing."""

    def metadata(entity, unit, device_class, name):
        """Supply only normal bounded discovery metadata, not arbitrary provider attributes."""
        return {
            "entity_id": entity,
            "unit": unit,
            "device_class": device_class,
            "name": name,
            "device_name": name,
        }

    return {
        "entities": [
            metadata("sensor.car_soc", "%", "battery", "Car A"),
            metadata("sensor.other_soc", "%", "battery", "Car B"),
            metadata("sensor.charger_energy", "kWh", "energy", "Wallbox"),
            metadata("sensor.charger_power", "W", "power", "Wallbox power"),
            metadata("binary_sensor.charger_connected", "", "plug", "Wallbox connected"),
        ],
        "suggestions": {
            "consumers": [
                {
                    "entity_id": "sensor.charger_energy",
                    "power_entity": "sensor.charger_power",
                    "upstream": None,
                }
            ]
        },
        "timezone": "Europe/Amsterdam",
    }


def settings():
    """Use 60 usable kWh, an 11-kW vehicle limit and a shared wallbox with explicit allowed cars."""
    return {
        "vehicles": [
            {
                "id": CAR,
                "soc_entity": "sensor.car_soc",
                "capacity_kwh": 60,
                "maximum_w": 11000,
                "target_soc": 80,
                "departure_time": "07:00",
            },
            {
                "id": OTHER_CAR,
                "soc_entity": "sensor.other_soc",
                "capacity_kwh": 40,
                "maximum_w": 7400,
                "target_soc": 70,
                "departure_time": "08:00",
            },
        ],
        "points": [
            {
                "id": POINT,
                "energy_entity": "sensor.charger_energy",
                "power_entity": "sensor.charger_power",
                "connected_entity": "binary_sensor.charger_connected",
                "minimum_w": 1400,
                "maximum_w": 22000,
                "control": "variable",
                "vehicle_ids": [CAR, OTHER_CAR],
            }
        ],
    }


def observations(service, now=NOW, *, connected=True, session=SESSION, soc=50):
    """Create one fresh HA capture with independent SoC readings and an explicit physical connection nonce."""
    return {
        "configuration_version": service.configuration_version,
        "at": now.isoformat(),
        "vehicles": [
            {
                "id": CAR,
                "soc": soc,
                "observed_at": now.isoformat() if soc is not None else None,
                "reported_at": now.isoformat() if soc is not None else None,
                "timestamp_kind": "ha_report" if soc is not None else None,
            },
            {
                "id": OTHER_CAR,
                "soc": 20,
                "observed_at": now.isoformat(),
                "reported_at": now.isoformat(),
                "timestamp_kind": "ha_report",
            },
        ],
        "points": [{"id": POINT, "session_id": session, "connected": connected, "power_w": 0}],
    }
