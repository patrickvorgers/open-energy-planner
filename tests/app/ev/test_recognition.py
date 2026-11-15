"""Vehicle identity follows available evidence without treating low charging power as identity."""

from datetime import timedelta
from types import SimpleNamespace

from open_energy_planner.appliances.ev.recognition import recognize
from tests.app.ev.support.devices import (
    CAR,
    NOW,
    OTHER_CAR,
    OTHER_POINT,
    POINT,
    SESSION,
    observations,
    settings,
)


def example():
    """Create a shared charger without assumed identity or a planned request."""
    return (
        {**settings(), "assignments": {}, "requests": {}, "active_requests": {}},
        observations(SimpleNamespace(configuration_version="fixture")),
    )


def signal(value, at=NOW):
    """Represent a real timestamped plug signal."""
    return {"value": value, "observed_at": at.isoformat()}


def test_single_car_needs_no_optional_sources():
    """One allowed car is automatically identified as soon as physical connection is known."""
    state, report = example()
    state["points"][0]["vehicle_ids"] = [CAR]
    assert recognize(state, report, NOW)[POINT]["vehicle_id"] == CAR


def test_low_power_is_ambiguous_but_excess_power_excludes_smaller_vehicle():
    """Both cars can charge at 3.5 kW. A 10-kW draw cannot belong to the 7.4-kW car."""
    state, report = example()
    report["points"][0]["power_w"] = 3500
    assert recognize(state, report, NOW)[POINT]["vehicle_id"] is None
    report["points"][0]["power_w"] = 10000
    assert recognize(state, report, NOW)[POINT]["vehicle_id"] == CAR


def test_fresh_plug_evidence_resolves_shared_charger_and_overrides_retained_identity():
    """The other car's current plug signal wins over a prior expectation on this connection."""
    state, report = example()
    state["assignments"][POINT] = {"vehicle_id": CAR, "session_id": SESSION}
    report["vehicles"][1]["connected"] = signal(True)
    assert recognize(state, report, NOW)[POINT]["vehicle_id"] == OTHER_CAR
    report["vehicles"][1]["connected"] = signal(True, NOW - timedelta(days=1))
    assert recognize(state, report, NOW)[POINT]["vehicle_id"] == CAR


def test_fresh_away_excludes_car_but_old_location_does_not():
    """A stale away report cannot exclude a car that may have arrived since that measurement."""
    state, report = example()
    report["vehicles"][0]["home"] = signal(False)
    assert recognize(state, report, NOW)[POINT]["vehicle_id"] == OTHER_CAR
    report["vehicles"][0]["home"] = signal(False, NOW - timedelta(days=1))
    assert recognize(state, report, NOW)[POINT]["vehicle_id"] is None


def test_planned_expectation_is_labelled_and_old_assignment_does_not_cross_connections():
    """A unique planned car is an expectation. An old manual assignment alone cannot name a new connection."""
    state, report = example()
    state["assignments"][POINT] = {"vehicle_id": CAR, "session_id": "previous-connection"}
    assert recognize(state, report, NOW)[POINT]["vehicle_id"] is None
    state["active_requests"][OTHER_CAR] = "request"
    state["requests"]["request"] = {
        "status": "planned",
        "departure": (NOW + timedelta(hours=1)).isoformat(),
    }
    assigned = recognize(state, report, NOW)[POINT]
    assert assigned["vehicle_id"] == OTHER_CAR
    assert assigned["origin"] == "planned_expectation"


def test_two_connected_points_cannot_both_claim_the_same_car():
    """Duplicate candidate ownership is ambiguous until evidence can distinguish the two sessions."""
    state, report = example()
    state["points"][0]["vehicle_ids"] = [CAR]
    state["points"].append({**state["points"][0], "id": OTHER_POINT})
    report["points"].append({**report["points"][0], "id": OTHER_POINT, "session_id": "other"})
    assigned = recognize(state, report, NOW)
    assert all(row["vehicle_id"] is None for row in assigned.values())


def test_retained_identity_cannot_resolve_competing_fresh_positive_signals():
    """Two plugged cars remain ambiguous even when one was previously assigned."""
    state, report = example()
    state["assignments"][POINT] = {"vehicle_id": CAR, "session_id": SESSION}
    for vehicle in report["vehicles"]:
        vehicle["connected"] = signal(True)
    assigned = recognize(state, report, NOW)[POINT]
    assert assigned["vehicle_id"] is None
    assert assigned["origin"] == "ambiguous"
