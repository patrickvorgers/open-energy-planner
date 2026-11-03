"""Compare exact archived forecasts with independently retained completed-hour measurements."""

from datetime import UTC, datetime, timedelta
from math import fsum

from open_energy_planner.core.units import WATTS_PER_KW
from open_energy_planner.demand.calculation.model import digest
from open_energy_planner.forecast_quality.calculation.metrics import series

from ..solar.learning import INVERTER_CLIPPING_THRESHOLD

ENERGY_TOLERANCE_KWH = 1e-7
MAX_VIEW_HOURS = 168


def measured(archive, sources, terms, start, end):
    """Require every frozen source identity and signed channel. Missing channels never become zero."""
    by_entity = {row["entity_id"]: row for row in sources}
    channels, receipts = [], []
    for term in terms:
        source = by_entity.get(term["entity_id"])
        if source is None:
            return {}, None
        values, receipt = archive.read(source, start, end)
        channels.append((term["sign"], values))
        if receipt:
            receipts.append(receipt)
    if not channels:
        return {}, None
    complete = set.intersection(*(set(values) for _sign, values in channels))
    result = {}
    for instant in complete:
        value = fsum(sign * values[instant] for sign, values in channels)
        if value >= -ENERGY_TOLERANCE_KWH:
            result[instant] = max(0, value)
    return result, max(receipts) if receipts else None


def evidence(plan, archive, demand_documents, solar_documents, now, *, window=None, names=None):
    """Resolve frozen forecasts and HA evidence without rerunning models or inferring historic knowledge.

    Args:
        plan: Original plan or frozen daily context with retained source bindings.
        archive: Independent cumulative hourly measurement archive in kWh.
        demand_documents: Original demand forecast and configuration documents.
        solar_documents: Original PV forecast and configuration documents.
        now: Aware evaluation time, reduced by the Recorder completion delay.
        window: Optional aware UTC (start, end) datetimes replacing interval-derived
            bounds and MAX_VIEW_HOURS. Callers must bound this window. Whole UTC
            hours align with measurements. Fractional-hour day edges lack actuals.
        names: Current Energy names keyed by the archived meter identity. Only
            labels change. Removed sources retain their captured name.

    Returns:
        Series, issues and evidence version. Daily contexts need no problem intervals
        and return None for their absent plan_id and generated_at.
    """
    result = {
        "plan_id": plan.get("plan_id"),
        "generated_at": plan.get("generated_at"),
        "as_of": now.isoformat(),
        "series": [],
        "issues": [],
    }
    snapshot = plan.get("input_snapshot", {})
    intervals = snapshot.get("problem", {}).get("intervals", [])
    sources = plan.get("forecast_quality_sources")
    if (not intervals and window is None) or sources is None:
        return {
            **result,
            "issues": ["This original plan predates retained forecast-quality source bindings"],
        }
    if window is not None:
        start, end = window
    else:
        start = (
            datetime.fromisoformat(intervals[0]["start"])
            .astimezone(UTC)
            .replace(minute=0, second=0, microsecond=0)
        )
        last = intervals[-1]
        finish = datetime.fromisoformat(last["start"]) + timedelta(hours=last["duration_h"])
        end = min(
            finish.replace(minute=0, second=0, microsecond=0),
            start + timedelta(hours=MAX_VIEW_HOURS),
        )
    for entry in snapshot.get("demand_evidence", []):
        version = entry["forecast_version"]
        try:
            path = (
                "consumer-forecast-" + version + ".json"
                if entry["model"] == "consumer-composite-v1"
                else "forecast-" + version + ".json"
            )
            forecast = demand_documents.read(path)["forecast"]
            if forecast["version"] != version:
                raise ValueError("Original demand forecast reference mismatch")
            configuration = demand_documents.read(
                "configuration-" + entry["configuration_version"] + ".json"
            )
            terms = [
                {"entity_id": row["entity_id"], "sign": row["sign"]}
                for row in configuration["terms"]
            ]
            if forecast.get("excluded_devices"):
                terms.extend(
                    {"entity_id": row["entity_id"], "sign": -1}
                    for row in configuration["exclusions"]
                )
            actual, receipt = measured(archive, sources, terms, start, end)
            title = (
                "Residual household consumption"
                if forecast.get("excluded_devices")
                else "Household consumption"
            )
            controlled = bool(plan.get("forecast_quality_controlled"))
            issue = (
                "Controlled EV/appliance schedules alter household timing. This comparison does not score model accuracy"
                if controlled
                else None
            )
            result["series"].append(
                series(
                    "household",
                    title,
                    forecast,
                    forecast["steps"],
                    actual,
                    start,
                    end,
                    now,
                    receipt=receipt,
                    issue=issue,
                )
            )
            component_path = "quality-components-" + version + ".json"
            if demand_documents.exists(component_path):
                reference = demand_documents.read(component_path)
                components = (
                    reference["components"]
                    if "components" in reference
                    else demand_documents.read(
                        "consumer-forecast-" + reference["version"] + ".json"
                    )["components"]
                )
                for device in components["devices"]:
                    entity = device["entity_id"]
                    actual, receipt = measured(
                        archive, sources, [{"entity_id": entity, "sign": 1}], start, end
                    )
                    controlled = entity in plan.get("forecast_quality_controlled", [])
                    issue = (
                        "Controlled operation changes consumption timing. This comparison does not score model accuracy"
                        if controlled
                        else None
                    )
                    row = series(
                        entity,
                        (names or {}).get(
                            entity, plan.get("forecast_quality_names", {}).get(entity, entity)
                        ),
                        device["forecast"],
                        device["forecast"]["steps"],
                        actual,
                        start,
                        end,
                        now,
                        receipt=receipt,
                        issue=issue,
                    )
                    row.update(
                        weather_sensitive=device["weather_sensitive"],
                        weather_features_used=device["weather_features_used"],
                        composition_selected=reference["selected"],
                        controlled=controlled,
                    )
                    result["series"].append(row)
        except (FileNotFoundError, KeyError, TypeError, ValueError):
            result["issues"].append("Original household or component forecast evidence unavailable")
    for entry in snapshot.get("solar_evidence", []):
        try:
            forecast = solar_documents.read("forecast-" + entry["forecast_version"] + ".json")[
                "forecast"
            ]
            if forecast["version"] != entry["forecast_version"]:
                raise ValueError("Original solar forecast reference mismatch")
            configuration = solar_documents.read(
                "configuration-" + entry["configuration_version"] + ".json"
            )
            models_path = "quality-source-models-" + entry["forecast_version"] + ".json"
            source_models = (
                solar_documents.read(models_path) if solar_documents.exists(models_path) else {}
            )
            inverters = {row["inverter_id"]: row for row in configuration["inverters"]}
            for source in configuration["sources"]:
                identity = source["source_id"]
                if identity not in entry["source_ids"]:
                    continue
                boundary = source.get("measurement_boundary", "ac")
                field = "ac_kwh" if boundary == "ac" else "dc_kwh"
                steps = [
                    {"start": row["start"], "duration_h": row["duration_h"], "kwh": row[field]}
                    for row in forecast["energies"]
                    if row["source_id"] == identity
                ]
                actual, receipt = measured(
                    archive, sources, [{"entity_id": source["entity_id"], "sign": 1}], start, end
                )
                inverter = inverters[source["inverter_id"]]
                shared = (
                    sum(
                        row["inverter_id"] == source["inverter_id"]
                        for row in configuration["sources"]
                    )
                    > 1
                )
                issue = (
                    "Available production is not independently comparable with curtailed, hybrid or shared-inverter metering"
                    if source["can_curtail"] or inverter["hybrid"] or shared
                    else None
                )
                maximum = (
                    inverter["ac_limit_w"] / WATTS_PER_KW * INVERTER_CLIPPING_THRESHOLD
                    if boundary == "ac"
                    else None
                )
                row = series(
                    identity,
                    (names or {}).get(
                        source["entity_id"],
                        plan.get("comparison_names", {}).get(identity, identity),
                    ),
                    {
                        **forecast,
                        "model": source_models.get(identity, forecast["model"]),
                    },
                    steps,
                    actual,
                    start,
                    end,
                    now,
                    issue=issue,
                    receipt=receipt,
                    maximum_comparable_kwh=maximum,
                )
                row["measurement_boundary"] = boundary.upper()
                result["series"].append(row)
        except (FileNotFoundError, KeyError, TypeError, ValueError):
            result["issues"].append("Original solar forecast evidence unavailable")
    result["evidence_version"] = digest(result["series"])
    return result
