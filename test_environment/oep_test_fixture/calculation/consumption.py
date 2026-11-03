"""Calculate complete appliance energy additions without loading HA state or history."""


def recorded_additions(series, anchors, before, after):
    """Sum unseeded device use only from complete readings after creation. Missing/reset active evidence stays unknown."""
    total = 0.0
    for entity, anchor in anchors.items():
        if after <= anchor:
            continue
        values = series.get(entity, {})
        if before < anchor or before not in values or after not in values:
            return None
        delta = values[after] - values[before]
        if delta < 0:
            return None
        total += delta
    return total
