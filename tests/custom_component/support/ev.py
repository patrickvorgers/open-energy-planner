"""Independent available EV evidence for mixed-device HA safety scenarios."""

from datetime import timedelta


def available_ev(peer, now):
    """Provide a valid current request, explicit connected session and local measured SoC."""
    peer.hass.states.async_set("binary_sensor.wallbox_connected", "on")
    peer.hass.states.async_set(
        "sensor.car_soc", 50, {"unit_of_measurement": "%", "device_class": "battery"}
    )
    peer.ev_sessions = {"wallbox": {"connected": True, "session_id": "connected-session"}}
    peer.ev = {
        "revision": "ev-revision",
        "vehicles": [
            {
                "id": "car",
                "soc_entity": "sensor.car_soc",
                "target_soc": 80,
                "maximum_w": 7000,
                "charging_request": {
                    "status": "planned",
                    "target_soc": 80,
                    "ready_at": (now - timedelta(minutes=1)).isoformat(),
                    "departure": (now + timedelta(hours=1)).isoformat(),
                },
            }
        ],
        "points": [
            {
                "id": "wallbox",
                "vehicle_id": "car",
                "control": "variable",
                "connected_entity": "binary_sensor.wallbox_connected",
                "maximum_w": 7000,
            }
        ],
    }
