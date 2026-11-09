"""Reconstruct advice actually active during metering, distinct from physical realization."""

from datetime import datetime, timedelta


def active_advice(frame, activations, plans):
    """Integrate each accepted plan only over its active, enabled and valid portion of the interval."""
    imported = exported = covered = 0.0
    sources = []
    curtailed = set()
    for index, activation in enumerate(activations):
        left = max(frame.start, datetime.fromisoformat(activation["activated_at"]))
        right = (
            min(frame.end, datetime.fromisoformat(activations[index + 1]["activated_at"]))
            if index + 1 < len(activations)
            else frame.end
        )
        if not activation["enabled"] or not activation["plan_id"] or left >= right:
            continue
        plan = plans.get_context(activation["plan_id"])
        right = (
            min(right, datetime.fromisoformat(plan["valid_until"]))
            if plan.get("valid_until")
            else right
        )
        if left >= right:
            continue
        sources.append(plan["plan_id"])
        for command in plan.get("steps", ()):
            if (
                datetime.fromisoformat(command["start"]) < right
                and datetime.fromisoformat(command["end"]) > left
            ):
                curtailed.update(
                    key
                    for key, value in command.get("devices", {}).items()
                    if value.get("kind") == "solar" and value.get("enabled") is False
                )
        for step, flow in zip(
            plan.get("input_snapshot", {}).get("problem", {}).get("intervals", ()),
            plan.get("expected_flows", ()),
        ):
            begin = datetime.fromisoformat(step["start"])
            end = begin + timedelta(hours=step["duration_h"])
            overlap_start, overlap_end = max(left, begin), min(right, end)
            if overlap_start < overlap_end and "grid_import" in flow:
                fraction = (overlap_end - overlap_start) / (end - begin)
                imported += fraction * flow["grid_import"]
                exported += fraction * flow["grid_export"]
                covered += (overlap_end - overlap_start).total_seconds()
    return {
        "plan_ids": list(dict.fromkeys(sources)),
        "import_kwh": imported,
        "export_kwh": exported,
        "coverage_seconds": covered,
        "complete": covered >= (frame.end - frame.start).total_seconds(),
        "possible_curtailment": sorted(curtailed),
    }
