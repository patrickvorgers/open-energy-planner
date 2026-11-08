"""Resolve presentation names independently of source identity and archived calculations."""

from open_energy_planner.savings.diagnostics import public_reference_plan


def source_name(metadata, fallback):
    """Prefer Energy's explicit name, then the HA device and sensor, never an opaque identity."""
    for key in ("energy_name", "device_name", "name"):
        name = metadata.get(key)
        if isinstance(name, str) and name.strip() and name.strip() != metadata.get("entity_id"):
            return name.strip()
    return fallback


def source_names(catalogue):
    """Return known current labels. Missing metadata must retain an archived name."""
    return {
        row["entity_id"]: name
        for row in (catalogue or {}).get("entities", ())
        if (name := source_name(row, None)) is not None
    }


def vehicle_source_names(catalogue):
    """Prefer the car's device name over an integration title that may identify an account."""
    return {
        row["entity_id"]: source_name(
            {"entity_id": row["entity_id"], "device_name": row.get("device_name")}, None
        )
        or row.get("integration_name", "").strip()
        or source_name(row, "Vehicle")
        for row in (catalogue or {}).get("entities", ())
    }


def energy_source_names(catalogue):
    """Limit historical overrides to current Energy members, retaining removed-source labels."""
    names = source_names(catalogue)
    hints = (catalogue or {}).get("suggestions")
    if hints is None:
        return names
    active = {row["entity_id"] for row in hints.get("terms", ())}
    active.update(row["entity_id"] for row in hints.get("consumers", ()))
    active.update(hints.get("solar_sources", ()))
    for battery in hints.get("batteries", ()):
        active.update(battery.get(key) for key in ("energy_entity", "charge_energy_entity"))
    return {entity: name for entity, name in names.items() if entity in active}


def display_devices(devices, catalogue):
    """Project names independently of Energy readiness without changing configured membership."""
    if devices is None:
        return None
    names = source_names(catalogue)
    return [
        {
            **device,
            "name": names.get(
                device.get("energy_entity"),
                {"battery": "Battery", "solar": "Solar"}[device["kind"]],
            ),
        }
        for device in devices
    ]


def display_plan(plan, catalogue):
    """Project safe display metadata, matching stable IDs through the plan's frozen source links.

    The original plan, forecast references, metering bindings and numerical values
    remain untouched. Removed sources retain the names captured in that plan.
    """
    if plan is None:
        return None
    plan = public_reference_plan(plan)
    names = energy_source_names(catalogue)
    links = dict(plan.get("display_sources", {}))
    bindings = plan.get("comparison_bindings", {})
    links.update(
        {row["device_id"]: row.get("energy_entity") for row in bindings.get("batteries", ())}
    )
    links.update({row["source_id"]: row["entity_id"] for row in bindings.get("solar", ())})
    return {
        **plan,
        "comparison_names": {
            **plan.get("comparison_names", {}),
            **{identity: names[entity] for identity, entity in links.items() if entity in names},
        },
        "forecast_quality_names": {
            **plan.get("forecast_quality_names", {}),
            **{
                entity: names[entity]
                for entity in plan.get("forecast_quality_names", {})
                if entity in names
            },
        },
    }


def comparison_names(catalogue, thermal_state, ev_state, installation, solar):
    """Capture labels by stable device identity without changing frozen metering bindings."""
    names = source_names(catalogue)
    vehicles = vehicle_source_names(catalogue)
    return {
        **{
            store["id"]: names.get(store["energy_entity"], "Thermal storage")
            for store in thermal_state["stores"]
        },
        **{car["id"]: vehicles.get(car["soc_entity"], "Vehicle") for car in ev_state["vehicles"]},
        **{
            point["id"]: names.get(point["energy_entity"], "Charge point")
            for point in ev_state["points"]
        },
        **{
            battery.device_id: names.get(battery.energy_entity, "Battery")
            for battery in installation.batteries
        },
        **{source.source_id: names.get(source.entity_id, "Solar") for source in solar.sources},
    }
