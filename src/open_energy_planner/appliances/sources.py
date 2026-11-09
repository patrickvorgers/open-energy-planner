"""Validate disjoint Home Assistant Energy ownership for every appliance model."""

SOURCE_ALREADY_ASSIGNED = (
    "This Energy device is already added as an appliance. "
    "Edit or remove it in its existing category first."
)


def validate_new_assignments(requested, retained, selected, reserved):
    """Reject meter reuse while permitting edits to the same persisted appliance and meter.

    Call under the shared settings lock before any write. Forecast selection and
    specialized records both reserve a source, including after an interrupted
    specialized save or a missing forecast. Display names never establish identity.
    Existing shape and Energy-membership validation remains authoritative.
    """
    occupied = {row["entity_id"] for row in selected}
    other_categories = {row["energy_entity"] for row in reserved}
    previous = {(row["id"], row["energy_entity"]) for row in retained}
    for row in requested:
        if row["energy_entity"] in other_categories or (
            row["energy_entity"] in occupied and (row["id"], row["energy_entity"]) not in previous
        ):
            raise ValueError(SOURCE_ALREADY_ASSIGNED)


def validate_membership(selected, options):
    """Reject unknown meters, cycles and any selected ancestor after every Energy change."""
    by_entity = {item["entity_id"]: item for item in options}
    identities = {item["entity_id"] for item in selected}
    if not identities <= set(by_entity):
        raise ValueError("Select consumers configured in Home Assistant Energy")
    for entity in identities:
        visited = {entity}
        parent = by_entity[entity].get("upstream")
        while parent:
            if parent in identities or parent in visited:
                raise ValueError("Do not select overlapping upstream/downstream meters")
            visited.add(parent)
            if parent not in by_entity:
                raise ValueError("Upstream Energy meter is missing")
            parent = by_entity[parent].get("upstream")
