"""Retain real SoC anchors and session-owned cumulative charging energy in the active database."""

from copy import deepcopy

from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.ev.model import delivered_power
from open_energy_planner.core.units import FULL_SOC_PERCENT, SECONDS_PER_HOUR, WATTS_PER_KW

TELEMETRY_KEY = "ev-telemetry.json"
ENERGY_TOLERANCE_KWH = 0.01
POWER_TOLERANCE_FRACTION = 0.1
MEASURED_SOC_MAX_AGE_SECONDS = 300


def empty_telemetry():
    """Return a bounded current pointer, separate from immutable user-intent history."""
    return {"vehicles": {}, "sessions": {}}


def configure_telemetry(previous, before, after):
    """Keep same-source physical-session facts when editing unrelated vehicle preferences."""
    updated = deepcopy(previous)
    cars = {car["id"]: car for car in after["vehicles"]}
    unchanged = {
        car["id"]
        for car in before["vehicles"]
        if car["id"] in cars and car["soc_entity"] == cars[car["id"]]["soc_entity"]
    }
    updated["vehicles"] = {
        vehicle_id: evidence
        for vehicle_id, evidence in updated["vehicles"].items()
        if vehicle_id in unchanged and evidence["source_entity"] == cars[vehicle_id]["soc_entity"]
    }
    old_points = {point["id"]: point for point in before["points"]}
    updated["sessions"] = {
        point["id"]: updated["sessions"][point["id"]]
        for point in after["points"]
        if point["id"] in updated["sessions"]
        and point["id"] in old_points
        and point["connected_entity"] == old_points[point["id"]]["connected_entity"]
    }
    for session in updated["sessions"].values():
        session["request_ids"] = {
            vehicle_id: request_id
            for vehicle_id, request_id in session["request_ids"].items()
            if vehicle_id in cars
        }
        if session["vehicle_id"] not in unchanged:
            session.update(vehicle_id=None, attributed_at=None)
        if session["credited_vehicle_id"] not in unchanged:
            session.update(credited_vehicle_id=None, credited_kwh=0.0)
    for point in after["points"]:
        session = updated["sessions"].get(point["id"])
        if session and any(
            point[field] != old_points[point["id"]][field]
            for field in ("energy_entity", "power_entity")
        ):
            session.update(
                energy_entity=point["energy_entity"],
                energy_kwh=None,
                vehicle_id=None,
                attributed_at=None,
            )
    return updated


def update_telemetry(previous, state, report):
    """Apply each real sample or meter delta once, never a plan's predicted energy.

    New real SoC replaces the estimate. Only whole meter intervals after that sample count.
    An unchanged value with only a newer HA publication time is not a new sample.
    Counter resets, source changes and new connection nonces establish baselines
    instead of attributing an unproved delta to a vehicle. The original sample
    remains available even when a later capture has no usable SoC.
    """
    updated = deepcopy(previous)
    cars = {row["id"]: row for row in state["vehicles"]}
    updated["vehicles"] = {
        vehicle_id: evidence
        for vehicle_id, evidence in updated["vehicles"].items()
        if vehicle_id in cars and evidence["source_entity"] == cars[vehicle_id]["soc_entity"]
    }
    new_samples = set()
    for reading in report["vehicles"]:
        if reading["soc"] is None:
            continue
        old = updated["vehicles"].get(reading["id"])
        newer = old is None or instant(reading["observed_at"]) >= instant(old["observed_at"])
        changed = old is None or reading["soc"] != old["measured_soc"]
        sampled = (
            old is not None
            and reading["timestamp_kind"] == "device_sample"
            and instant(reading["observed_at"]) > instant(old["observed_at"])
        )
        if newer and (changed or sampled):
            updated["vehicles"][reading["id"]] = {
                **{
                    field: reading.get(field)
                    for field in (
                        "observed_at",
                        "reported_at",
                        "timestamp_kind",
                        "source_timestamp_attribute",
                    )
                },
                "source_entity": cars[reading["id"]]["soc_entity"],
                "measured_soc": reading["soc"],
                "stored_kwh": 0.0,
                "estimate_updated_at": report["at"],
            }
            new_samples.add(reading["id"])
    configured = {row["id"]: row for row in state["points"]}
    sessions = {}
    for point in report["points"]:
        previous_session = updated["sessions"].get(point["id"], {})
        assigned = state["assignments"].get(point["id"], {})
        vehicle_id = (
            assigned.get("vehicle_id")
            if point["connected"] is True and assigned.get("session_id") == point["session_id"]
            else None
        )
        same_session = previous_session.get("session_id") == point["session_id"]
        old_vehicle = previous_session.get("credited_vehicle_id") if same_session else None
        credited = previous_session.get("credited_kwh", 0.0) if same_session else 0.0
        if old_vehicle in new_samples:
            credited = 0.0
        if old_vehicle and vehicle_id and old_vehicle != vehicle_id:
            old_evidence = updated["vehicles"].get(old_vehicle)
            if old_evidence:
                old_evidence["stored_kwh"] = max(
                    0.0,
                    old_evidence["stored_kwh"] - credited,
                )
            credited = 0.0
        meter_at = point.get("energy_observed_at") or report["at"]
        continuous = (
            same_session
            and vehicle_id is not None
            and previous_session.get("vehicle_id") == vehicle_id
            and previous_session.get("energy_entity") == configured[point["id"]]["energy_entity"]
        )
        checkpoint = {
            "session_id": point["session_id"],
            "vehicle_id": vehicle_id,
            "attributed_at": previous_session["attributed_at"]
            if continuous
            else report["at"]
            if vehicle_id
            else None,
            "energy_entity": configured[point["id"]]["energy_entity"],
            "energy_kwh": point.get("energy_kwh"),
            "energy_at": meter_at,
            "at": report["at"],
            "credited_vehicle_id": vehicle_id or old_vehicle,
            "credited_kwh": credited,
            "request_ids": deepcopy(previous_session["request_ids"]) if same_session else {},
            "energy_issue": None,
        }
        evidence = updated["vehicles"].get(vehicle_id)
        if continuous:
            baseline_at = previous_session["energy_at"]
            current_energy, previous_energy = (
                point.get("energy_kwh"),
                previous_session.get("energy_kwh"),
            )
            if current_energy is None or current_energy == previous_energy:
                checkpoint["energy_at"] = baseline_at
                if current_energy is not None and point.get("energy_observed_at") and evidence:
                    credit_from = max(
                        instant(checkpoint["attributed_at"]), instant(evidence["observed_at"])
                    )
                    if instant(baseline_at) < credit_from <= instant(meter_at):
                        checkpoint["energy_at"] = meter_at
            if current_energy is None:
                checkpoint.update(
                    energy_kwh=previous_energy,
                    energy_issue="meter_unavailable",
                )
            elif previous_energy is not None and evidence:
                delta = current_energy - previous_energy
                hours = (instant(meter_at) - instant(baseline_at)).total_seconds()
                hours /= SECONDS_PER_HOUR
                maximum = min(configured[point["id"]]["maximum_w"], cars[vehicle_id]["maximum_w"])
                limit = maximum * hours / WATTS_PER_KW
                if hours < 0:
                    checkpoint.update(
                        energy_kwh=previous_energy,
                        energy_at=baseline_at,
                        energy_issue="meter_discontinuity",
                    )
                elif delta < 0:
                    checkpoint["energy_issue"] = "meter_reset"
                elif delta > limit * (1 + POWER_TOLERANCE_FRACTION) + ENERGY_TOLERANCE_KWH:
                    checkpoint["energy_issue"] = "meter_discontinuity"
                elif (
                    delta > 0
                    and hours > 0
                    and instant(baseline_at) >= instant(evidence["observed_at"])
                    and instant(baseline_at) >= instant(checkpoint["attributed_at"])
                ):
                    average_w = min(maximum, delta * WATTS_PER_KW / hours)
                    useful = delta * delivered_power(average_w) / average_w
                    evidence["stored_kwh"] += useful
                    evidence["estimate_updated_at"] = report["at"]
                    checkpoint["credited_kwh"] += useful
        sessions[point["id"]] = checkpoint
    updated["sessions"] = sessions
    return updated


def vehicle_estimates(telemetry, vehicles, now):
    """Expose estimates with original source age and explicit unobserved-driving limitations."""
    results = {}
    for vehicle in vehicles:
        evidence = telemetry["vehicles"].get(vehicle["id"])
        if not evidence or evidence["source_entity"] != vehicle["soc_entity"]:
            continue
        estimated = min(
            FULL_SOC_PERCENT,
            evidence["measured_soc"]
            + evidence["stored_kwh"] * FULL_SOC_PERCENT / vehicle["capacity_kwh"],
        )
        age = max(0, (now - instant(evidence["observed_at"])).total_seconds())
        basis = (
            "estimated_from_meter"
            if evidence["stored_kwh"] > 0
            else "last_known"
            if age > MEASURED_SOC_MAX_AGE_SECONDS
            else "measured"
        )
        results[vehicle["id"]] = {
            "id": vehicle["id"],
            "soc": estimated,
            **evidence,
            "basis": basis,
            "sample_age_seconds": age,
        }
    return results
