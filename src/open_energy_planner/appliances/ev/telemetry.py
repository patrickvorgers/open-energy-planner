"""Retain real SoC anchors and session-owned cumulative charging energy in the active database."""

from copy import deepcopy

from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.ev.model import (
    delivered_power,
    delivery_points,
    vehicle_efficiency_stages,
)
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
        car["id"]: cars[car["id"]]
        for car in before["vehicles"]
        if car["id"] in cars and car["soc_entity"] == cars[car["id"]]["soc_entity"]
    }
    updated["vehicles"] = _retain_vehicle_evidence(updated["vehicles"], unchanged)
    old_points = {point["id"]: point for point in before["points"]}
    sessions = {}
    for point in after["points"]:
        old_point = old_points.get(point["id"])
        session = updated["sessions"].get(point["id"])
        if old_point is None or session is None:
            continue
        if point["connected_entity"] != old_point["connected_entity"]:
            continue
        _reconcile_session_sources(session, point, old_point, cars, unchanged)
        sessions[point["id"]] = session
    updated["sessions"] = sessions
    return updated


def _retain_vehicle_evidence(evidence_by_vehicle, cars):
    """Keep anchors whose configured vehicle and SoC entity still match."""
    return {
        vehicle_id: evidence
        for vehicle_id, evidence in evidence_by_vehicle.items()
        if vehicle_id in cars and evidence["source_entity"] == cars[vehicle_id]["soc_entity"]
    }


def _reconcile_session_sources(session, point, old_point, cars, unchanged_cars):
    """Invalidate evidence that depends on a changed SoC, meter or power source."""
    session["request_ids"] = {
        vehicle_id: request_id
        for vehicle_id, request_id in session["request_ids"].items()
        if vehicle_id in cars
    }
    if session["vehicle_id"] not in unchanged_cars:
        session.update(vehicle_id=None, attributed_at=None)
    if session["credited_vehicle_id"] not in unchanged_cars:
        session.update(credited_vehicle_id=None, credited_kwh=0.0)
    if any(point[field] != old_point[field] for field in ("energy_entity", "power_entity")):
        session.update(
            energy_entity=point["energy_entity"],
            energy_kwh=None,
            vehicle_id=None,
            attributed_at=None,
        )


def _credit_starts_at(checkpoint, evidence):
    """Find when both the session attribution and measured SoC support meter credit."""
    return max(instant(checkpoint["attributed_at"]), instant(evidence["observed_at"]))


def _retain_unchanged_meter_time(checkpoint, previous_session, evidence, reading):
    """Keep an equal counter's time unless a real observation establishes usable attribution."""
    energy = reading.get("energy_kwh")
    if energy is None or energy != previous_session.get("energy_kwh"):
        return
    baseline_at = previous_session["energy_at"]
    observed_at = reading.get("energy_observed_at")
    if observed_at and evidence:
        credit_from = _credit_starts_at(checkpoint, evidence)
        if instant(baseline_at) < credit_from <= instant(observed_at):
            return
    checkpoint["energy_at"] = baseline_at


def _apply_meter_delta(checkpoint, previous_session, evidence, charger, vehicle):
    """Advance a continuous meter checkpoint and credit only provable delivered energy.

    The checkpoint and vehicle evidence belong to the caller's detached copy.
    Missing, reset and implausible readings affect the checkpoint without adding energy.
    """
    baseline_at = previous_session["energy_at"]
    current_energy = checkpoint["energy_kwh"]
    previous_energy = previous_session.get("energy_kwh")
    if current_energy is None:
        checkpoint.update(
            energy_kwh=previous_energy,
            energy_at=baseline_at,
            energy_issue="meter_unavailable",
        )
        return
    if previous_energy is None or not evidence:
        return
    delta = current_energy - previous_energy
    baseline = instant(baseline_at)
    hours = (instant(checkpoint["energy_at"]) - baseline).total_seconds() / SECONDS_PER_HOUR
    maximum = min(charger["maximum_w"], vehicle["maximum_w"])
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
    elif delta > 0 and hours > 0 and baseline >= _credit_starts_at(checkpoint, evidence):
        average_w = min(maximum, delta * WATTS_PER_KW / hours)
        points = delivery_points(vehicle_efficiency_stages(vehicle))
        useful = delta * delivered_power(average_w, points) / average_w
        evidence["stored_kwh"] += useful
        evidence["estimate_updated_at"] = checkpoint["at"]
        checkpoint["credited_kwh"] += useful


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
    vehicles = _retain_vehicle_evidence(updated["vehicles"], cars)
    updated["vehicles"] = vehicles
    new_samples = _observe_vehicle_samples(vehicles, cars, report)
    chargers = {row["id"]: row for row in state["points"]}
    sessions = {}
    for reading in report["points"]:
        point_id = reading["id"]
        previous_session = updated["sessions"].get(point_id, {})
        assignment = state["assignments"].get(point_id, {})
        charger = chargers[point_id]
        checkpoint, continuous = _session_checkpoint(
            reading, previous_session, assignment, charger, report["at"]
        )
        _reconcile_session_credit(checkpoint, vehicles, new_samples)
        if continuous:
            vehicle_id = checkpoint["vehicle_id"]
            evidence = vehicles.get(vehicle_id)
            _apply_meter_delta(
                checkpoint, previous_session, evidence, charger, cars.get(vehicle_id)
            )
            _retain_unchanged_meter_time(checkpoint, previous_session, evidence, reading)
        sessions[point_id] = checkpoint
    updated["sessions"] = sessions
    return updated


def _observe_vehicle_samples(vehicles, cars, report):
    """Replace SoC anchors only with new evidence and return the vehicles that were reset."""
    new_samples = set()
    for reading in report["vehicles"]:
        if reading["soc"] is None:
            continue
        vehicle_id = reading["id"]
        old = vehicles.get(vehicle_id)
        if old is not None:
            observed_at = instant(reading["observed_at"])
            previous_at = instant(old["observed_at"])
            if observed_at < previous_at:
                continue
            if reading["soc"] == old["measured_soc"] and (
                reading["timestamp_kind"] != "device_sample" or observed_at == previous_at
            ):
                continue
        vehicles[vehicle_id] = {
            **{
                field: reading.get(field)
                for field in (
                    "observed_at",
                    "reported_at",
                    "timestamp_kind",
                    "source_timestamp_attribute",
                )
            },
            "source_entity": cars[vehicle_id]["soc_entity"],
            "measured_soc": reading["soc"],
            "stored_kwh": 0.0,
            "estimate_updated_at": report["at"],
        }
        new_samples.add(vehicle_id)
    return new_samples


def _session_checkpoint(reading, previous_session, assignment, charger, captured_at):
    """Build a detached checkpoint and identify intervals from the same meter and attribution."""
    session_id = reading["session_id"]
    vehicle_id = (
        assignment.get("vehicle_id")
        if reading["connected"] is True and assignment.get("session_id") == session_id
        else None
    )
    same_session = previous_session.get("session_id") == session_id
    continuous = (
        same_session
        and vehicle_id is not None
        and previous_session.get("vehicle_id") == vehicle_id
        and previous_session.get("energy_entity") == charger["energy_entity"]
    )
    attributed_at = captured_at if vehicle_id else None
    if continuous:
        attributed_at = previous_session["attributed_at"]
    return {
        "session_id": session_id,
        "vehicle_id": vehicle_id,
        "attributed_at": attributed_at,
        "energy_entity": charger["energy_entity"],
        "energy_kwh": reading.get("energy_kwh"),
        "energy_at": reading.get("energy_observed_at") or captured_at,
        "at": captured_at,
        "credited_vehicle_id": previous_session.get("credited_vehicle_id")
        if same_session
        else None,
        "credited_kwh": previous_session.get("credited_kwh", 0.0) if same_session else 0.0,
        "request_ids": deepcopy(previous_session["request_ids"]) if same_session else {},
        "energy_issue": None,
    }, continuous


def _reconcile_session_credit(checkpoint, vehicles, new_samples):
    """Keep proven credit through identity loss and undo it once on positive reassignment."""
    old_vehicle = checkpoint["credited_vehicle_id"]
    vehicle_id = checkpoint["vehicle_id"]
    credited = checkpoint["credited_kwh"]
    if old_vehicle in new_samples:
        credited = 0.0
    if old_vehicle and vehicle_id and old_vehicle != vehicle_id:
        old_evidence = vehicles.get(old_vehicle)
        if old_evidence:
            old_evidence["stored_kwh"] = max(0.0, old_evidence["stored_kwh"] - credited)
        credited = 0.0
    checkpoint["credited_vehicle_id"] = vehicle_id or old_vehicle
    checkpoint["credited_kwh"] = credited


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
