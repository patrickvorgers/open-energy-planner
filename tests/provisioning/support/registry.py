"""Provide ordinary example entity identities independently of Supervisor bootstrap."""

from script.provisioning import device_examples as examples


def example_registry(journal):
    """Return renamed virtual entities and states after recording their example ownership."""
    registry, states = [], []
    for name in ("dishwasher", "washing_machine", "dryer"):
        source = f"sensor.oep_test_{name}_energy"
        journal.state["appliances"][source] = {}
        virtual = "sensor.renamed_" + name
        registry.append(
            {
                "entity_id": virtual,
                "platform": "open_energy_planner",
                "unique_id": "entry_fixture_" + name + "_task_status",
                "device_id": "virtual-" + name,
            }
        )
        states.append({"entity_id": virtual, "attributes": {"source_entity": source}})
    for suffix in ("a", "b"):
        for key, domain in (("charging", "binary_sensor"), ("power", "sensor")):
            registry.append(
                {
                    "entity_id": domain + ".renamed_wallbox_" + suffix + "_" + key,
                    "platform": "open_energy_planner",
                    "unique_id": "entry_ev_" + examples.identity("wallbox-" + suffix) + "_" + key,
                }
            )
    registry.append(
        {
            "entity_id": "binary_sensor.renamed_boiler",
            "platform": "open_energy_planner",
            "unique_id": "entry_thermal_" + examples.identity("boiler") + "_requested_heating",
        }
    )
    journal.complete("owns:ev")
    journal.complete("owns:thermal")
    return registry, states
