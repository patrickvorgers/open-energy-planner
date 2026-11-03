"""Build owned example automation definitions without calling Supervisor or writing HA configuration."""

from script.provisioning.device_examples import (
    PROFILE_FILE,
    identity,
)


def automations(registry, states, journal):
    """Resolve virtual entities from registered identities and instantiate the shipped blueprints."""
    import json

    rows = [
        row
        for row in registry
        if row.get("platform") == "open_energy_planner" and not row.get("disabled_by")
    ]

    def entity(suffix):
        """Require exactly one enabled entity for an app-owned technical identity."""
        matches = [row["entity_id"] for row in rows if row["unique_id"].endswith(suffix)]
        if len(matches) != 1:
            raise LookupError("Waiting for a unique virtual simulator entity")
        return matches[0]

    def blueprint(key, name, filename, inputs):
        """Name one owned automation while retaining all execution semantics in the release blueprint."""
        return key, {
            "alias": "OEP lab: " + name,
            "description": "Development simulation only. No physical equipment is selected.",
            "use_blueprint": {"path": "open_energy_planner/" + filename + ".yaml", "input": inputs},
        }

    definitions = []
    for name in json.loads(PROFILE_FILE.read_text()):
        source = f"sensor.oep_test_{name}_energy"
        key = "automation:" + name
        if source not in journal.state["appliances"] or key in journal.state["completed"]:
            continue
        candidates = {
            row["entity_id"]: row for row in rows if row["unique_id"].endswith("_task_status")
        }
        matches = [
            candidates[state["entity_id"]]
            for state in states
            if state["entity_id"] in candidates
            and state.get("attributes", {}).get("source_entity") == source
        ]
        if len(matches) != 1 or not matches[0].get("device_id"):
            raise LookupError("Waiting for a virtual appliance device")
        inputs = {
            "appliance": matches[0]["device_id"],
            "start_action": [{"action": "oep_test_fixture." + name + "_start"}],
            "cancel_action": [{"action": "oep_test_fixture." + name + "_cancel"}],
        }
        for field, status in (
            ("started_trigger", "running"),
            ("completed_trigger", "completed"),
            ("cancelled_trigger", "cancelled"),
        ):
            inputs[field] = [
                {"trigger": "state", "entity_id": f"sensor.oep_test_{name}_status", "to": status}
            ]
        definitions.append(blueprint(key, name.replace("_", " "), "appliance", inputs))
    if "owns:ev" in journal.state["completed"]:
        for index, suffix in enumerate(("a", "b")):
            key = "automation:wallbox-" + suffix
            if key in journal.state["completed"]:
                continue
            point = identity("wallbox-" + suffix)
            inputs = {
                "permission": entity("_ev_" + point + "_charging"),
                "power": entity("_ev_" + point + "_power"),
                "charge_action": [
                    {
                        "action": "oep_test_fixture.ev_power",
                        "data": {"point": index, "power_w": "{{ requested_power_w }}"},
                    }
                ],
                "stop_action": [
                    {"action": "oep_test_fixture.ev_power", "data": {"point": index, "power_w": 0}}
                ],
            }
            definitions.append(blueprint(key, "wallbox " + suffix.upper(), "ev", inputs))
    if (
        "owns:thermal" in journal.state["completed"]
        and "automation:boiler" not in journal.state["completed"]
    ):
        inputs = {
            "permission": entity("_thermal_" + identity("boiler") + "_requested_heating"),
            "heat_action": [
                {"action": "oep_test_fixture.boiler_heater", "data": {"enabled": True}}
            ],
            "stop_action": [
                {"action": "oep_test_fixture.boiler_heater", "data": {"enabled": False}}
            ],
        }
        definitions.append(blueprint("automation:boiler", "electric boiler", "thermal", inputs))
    return definitions
