"""Verify persisted example-device behavior without owning Supervisor lifecycle commands."""


def verify_device_examples(app_request, states):
    """Check independent expected profiles, saved configurations and native automation states."""
    consumers = app_request("consumers")
    selected = {row["entity_id"]: row for row in consumers["selected"]}
    assert selected["sensor.oep_test_heat_pump_energy"]["weather_sensitive"] is True
    devices = {row["entity_id"]: row for row in app_request("appliances")["devices"]}
    for name, quarter_hour_count, energy_kwh in (
        ("dishwasher", 7, 0.83),
        ("washing_machine", 9, 0.92),
        ("dryer", 12, 1.40),
    ):
        device = devices[f"sensor.oep_test_{name}_energy"]
        assert device["shiftable"] and not device.get("task")
        profile = device["profile"]
        assert profile["evidence"] == ("manual" if name == "dryer" else "synthetic")
        assert profile["samples"] == (1 if name == "dryer" else 2)
        assert device["profile_kind"] == ("Manual" if name == "dryer" else "Learned")
        assert len(device["recent_cycles"]) == (1 if name == "dryer" else 2)
        assert len(profile["power_w"]) == quarter_hour_count
        assert abs(sum(profile["power_w"]) / 4000 - energy_kwh) < 0.000001
    charging = app_request("ev")
    assert len(charging["vehicles"]) == len(charging["points"]) == 2
    assert len(app_request("thermal")["configuration"]["stores"]) == 1
    contract = app_request("contracts")
    assert contract["periods"][0]["import_formula"] == "(market + 0.09161) * 1.21 + 0.02"
    assert len(contract["bonuses"]) == 1 and contract["bonuses"][0]["cap_kwh"] == 7500
    automation_states = {
        row["attributes"].get("id"): row["state"]
        for row in states
        if row["entity_id"].startswith("automation.")
    }
    for name in ("dishwasher", "washing_machine", "dryer", "wallbox_a", "wallbox_b", "boiler"):
        assert automation_states.get("oep_lab_" + name) == "on"
