"""Validate disjoint Home Assistant Energy ownership for every appliance model."""


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
