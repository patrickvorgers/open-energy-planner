"""Independent energy, timestamp and session ownership examples for retained vehicle SoC."""

from datetime import timedelta

import pytest

from open_energy_planner.appliances.ev.telemetry import (
    empty_telemetry,
    update_telemetry,
    vehicle_estimates,
)
from tests.app.ev.support.devices import CAR, NOW, OTHER_CAR, POINT, SESSION, observations, settings


def example():
    """Start at a known 50 percent and a 100-kWh charger counter with one assigned car."""
    state = {**settings(), "assignments": {POINT: {"vehicle_id": CAR, "session_id": SESSION}}}
    report = observations(type("Service", (), {"configuration_version": "fixture"})())
    report["points"][0]["energy_kwh"] = 100
    return state, report, update_telemetry(empty_telemetry(), state, report)


def test_republication_does_not_reset_charging_estimate():
    """One hour at 2.3 kW adds 1.84 battery kWh to 60 kWh, despite a repeated 50-percent report."""
    state, report, telemetry = example()
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["vehicles"][0].update(observed_at=report["at"], reported_at=report["at"])
    report["points"][0]["energy_kwh"] = 102.3
    previous = telemetry
    telemetry = update_telemetry(previous, state, report)
    assert previous["vehicles"][CAR]["stored_kwh"] == 0
    assert previous["sessions"][POINT]["energy_kwh"] == 100
    estimate = vehicle_estimates(telemetry, state["vehicles"], NOW + timedelta(hours=1))[CAR]
    assert estimate["soc"] == pytest.approx(50 + 1.84 / 60 * 100)
    assert estimate["observed_at"] == NOW.isoformat()
    assert estimate["basis"] == "estimated_from_meter"
    assert update_telemetry(telemetry, state, report) == telemetry


@pytest.mark.parametrize("counter, issue", [(1, "meter_reset"), (150, "meter_discontinuity")])
def test_invalid_energy_delta_reanchors_without_credit(counter, issue):
    """A reset or impossible 50-kWh jump over one hour cannot create battery energy."""
    state, report, telemetry = example()
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["points"][0]["energy_kwh"] = counter
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == 0
    assert telemetry["sessions"][POINT]["energy_issue"] == issue
    assert telemetry["sessions"][POINT]["energy_kwh"] == counter


def test_missing_energy_bridges_only_same_connection():
    """A missing capture preserves the baseline. A reconnect cannot inherit its unknown energy."""
    state, report, telemetry = example()
    report["at"] = (NOW + timedelta(minutes=30)).isoformat()
    report["points"][0]["energy_kwh"] = None
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["sessions"][POINT]["energy_kwh"] == 100
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["points"][0]["energy_kwh"] = 102.3
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(1.84)
    report["points"][0]["session_id"] = "new-connection"
    report["points"][0]["energy_kwh"] = 105
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(1.84)


def test_real_new_sample_corrects_estimate_and_old_anchor_never_expires():
    """A real device sample replaces accumulated energy. Years of age never erase known SoC."""
    state, report, telemetry = example()
    telemetry["vehicles"][CAR]["stored_kwh"] = 6
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["vehicles"][0].update(soc=52, observed_at=report["at"], timestamp_kind="device_sample")
    telemetry = update_telemetry(telemetry, state, report)
    estimate = vehicle_estimates(telemetry, state["vehicles"], NOW + timedelta(days=730))[CAR]
    assert estimate["soc"] == 52
    assert estimate["stored_kwh"] == 0
    assert estimate["basis"] == "last_known"


@pytest.mark.parametrize("connected", [False, None, True])
def test_unknown_identity_preserves_previously_attributable_energy(connected):
    """An outage cannot undo real charge. Recovery never attributes the ambiguous gap."""
    state, report, telemetry = example()
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["points"][0]["energy_kwh"] = 102.3
    telemetry = update_telemetry(telemetry, state, report)
    state["assignments"] = {}
    report["at"] = (NOW + timedelta(hours=2)).isoformat()
    report["points"][0].update(connected=connected, energy_kwh=104.6)
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(1.84)
    state["assignments"][POINT] = {"vehicle_id": CAR, "session_id": SESSION}
    report["at"] = (NOW + timedelta(hours=3)).isoformat()
    report["points"][0].update(connected=True, energy_kwh=106.9)
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(1.84)


def test_confirmed_reassignment_withdraws_old_session_credit_once():
    """Positive attribution to another car corrects the previous car without assigning the gap."""
    state, report, telemetry = example()
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["points"][0]["energy_kwh"] = 102.3
    telemetry = update_telemetry(telemetry, state, report)
    state["assignments"][POINT]["vehicle_id"] = OTHER_CAR
    report["at"] = (NOW + timedelta(hours=2)).isoformat()
    report["points"][0]["energy_kwh"] = 104.6
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == 0
    assert telemetry["vehicles"][OTHER_CAR]["stored_kwh"] == 0


def test_sparse_counter_updates_use_meter_baseline_time():
    """A minute of 11-kW charging is 0.183333 AC kWh and 0.165 battery kWh, regardless of capture frequency."""
    state, report, telemetry = example()
    for seconds in (5, 30, 55):
        report["at"] = (NOW + timedelta(seconds=seconds)).isoformat()
        telemetry = update_telemetry(telemetry, state, report)
    report["at"] = (NOW + timedelta(seconds=60)).isoformat()
    report["points"][0]["energy_kwh"] = 100 + 11 / 60
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(0.165)
    assert telemetry["sessions"][POINT]["energy_issue"] is None


def test_energy_sample_timestamp_does_not_become_capture_time():
    """The actual one-minute 11-kW meter interval remains one minute when its packet arrives late."""
    state, report, telemetry = example()
    report["at"] = (NOW + timedelta(seconds=90)).isoformat()
    report["points"][0].update(
        energy_kwh=100 + 11 / 60, energy_observed_at=(NOW + timedelta(seconds=60)).isoformat()
    )
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(0.165)
    assert telemetry["sessions"][POINT]["energy_at"] == (NOW + timedelta(seconds=60)).isoformat()


def test_regressed_meter_timestamp_does_not_replace_known_baseline():
    """An older out-of-order meter value cannot create credit or move the known baseline backwards."""
    state, report, telemetry = example()
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["points"][0].update(
        energy_kwh=102.3, energy_observed_at=(NOW - timedelta(minutes=1)).isoformat()
    )
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == 0
    assert telemetry["sessions"][POINT]["energy_kwh"] == 100
    assert telemetry["sessions"][POINT]["energy_at"] == NOW.isoformat()


@pytest.mark.parametrize("sample_minutes, credited", [(30, 1.84), (60, 1.84), (90, 0)])
def test_delayed_genuine_soc_credits_only_whole_subsequent_meter_interval(sample_minutes, credited):
    """A delayed 55% anchor keeps the following known interval, never a prorated straddling interval."""
    state, report, telemetry = example()
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["points"][0]["energy_kwh"] = 102.3
    telemetry = update_telemetry(telemetry, state, report)
    report["at"] = (NOW + timedelta(hours=2)).isoformat()
    report["vehicles"][0].update(
        soc=55,
        timestamp_kind="device_sample",
        observed_at=(NOW + timedelta(minutes=sample_minutes)).isoformat(),
    )
    report["points"][0]["energy_kwh"] = 104.6
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(credited)
    assert vehicle_estimates(telemetry, state["vehicles"], NOW + timedelta(hours=2))[CAR][
        "soc"
    ] == pytest.approx(55 + credited / 60 * 100)
    assert update_telemetry(telemetry, state, report) == telemetry


@pytest.mark.parametrize("transition", ["new_connection", "identity_recovery"])
def test_stale_meter_baseline_cannot_credit_pre_attribution_energy(transition):
    """A 90-minute-old baseline crosses attribution at t=2h and contributes no gap energy at t=3h."""
    state, report, telemetry = example()
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["points"][0].update(energy_kwh=102.3, energy_observed_at=report["at"])
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(1.84)
    stale = (NOW + timedelta(minutes=90)).isoformat()
    if transition == "new_connection":
        report["points"][0]["session_id"] = "new-physical-connection"
        state["assignments"][POINT]["session_id"] = "new-physical-connection"
    else:
        state["assignments"] = {}
        report["at"] = stale
        report["points"][0].update(energy_kwh=104.6, energy_observed_at=stale)
        telemetry = update_telemetry(telemetry, state, report)
        state["assignments"][POINT] = {"vehicle_id": CAR, "session_id": SESSION}
    report["at"] = (NOW + timedelta(hours=2)).isoformat()
    report["points"][0].update(energy_kwh=104.6, energy_observed_at=stale)
    telemetry = update_telemetry(telemetry, state, report)
    report["at"] = (NOW + timedelta(hours=3)).isoformat()
    report["points"][0].update(energy_kwh=106.9, energy_observed_at=report["at"])
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(1.84)
    report["at"] = (NOW + timedelta(hours=4)).isoformat()
    report["points"][0].update(energy_kwh=109.2, energy_observed_at=report["at"])
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(3.68)
    assert update_telemetry(telemetry, state, report) == telemetry


@pytest.mark.parametrize("boundary", ["attribution", "soc_anchor"])
@pytest.mark.parametrize(
    "observation", ["explicit", "missing", "unavailable", "regressed", "before_boundary"]
)
def test_equal_meter_sample_can_establish_an_attributable_baseline(boundary, observation):
    """Only an explicit post-boundary 100-kWh sample proves the following 2.3-kWh charge."""
    state, report, telemetry = example()
    stale = (NOW - timedelta(minutes=1)).isoformat()
    telemetry["sessions"][POINT]["energy_at"] = stale
    if boundary == "soc_anchor":
        telemetry["sessions"][POINT]["attributed_at"] = (NOW - timedelta(minutes=2)).isoformat()
    report["at"] = (NOW + timedelta(minutes=1)).isoformat()
    if observation != "missing":
        report["points"][0]["energy_observed_at"] = {
            "explicit": report["at"],
            "unavailable": report["at"],
            "regressed": (NOW - timedelta(minutes=2)).isoformat(),
            "before_boundary": stale,
        }[observation]
    if observation == "unavailable":
        report["points"][0]["energy_kwh"] = None
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["sessions"][POINT]["energy_at"] == (
        report["at"] if observation == "explicit" else stale
    )
    assert telemetry["vehicles"][CAR]["stored_kwh"] == 0
    assert update_telemetry(telemetry, state, report) == telemetry
    report["at"] = (NOW + timedelta(minutes=61)).isoformat()
    report["points"][0].update(energy_kwh=102.3, energy_observed_at=report["at"])
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(
        1.84 if observation == "explicit" else 0
    )
    assert update_telemetry(telemetry, state, report) == telemetry


def test_explicit_equal_samples_preserve_an_already_attributable_sparse_baseline():
    """Explicit unchanged samples cannot shorten the valid minute carrying 0.183333 AC kWh."""
    state, report, telemetry = example()
    for seconds in (5, 30, 55):
        report["at"] = (NOW + timedelta(seconds=seconds)).isoformat()
        report["points"][0]["energy_observed_at"] = report["at"]
        telemetry = update_telemetry(telemetry, state, report)
        assert telemetry["sessions"][POINT]["energy_at"] == NOW.isoformat()
    report["at"] = (NOW + timedelta(seconds=60)).isoformat()
    report["points"][0].update(energy_kwh=100 + 11 / 60, energy_observed_at=report["at"])
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(0.165)
    assert update_telemetry(telemetry, state, report) == telemetry
