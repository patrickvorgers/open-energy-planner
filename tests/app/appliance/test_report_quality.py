"""Independent heartbeat coverage and statistical bootstrap protect learned appliance models."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest

from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.cycles.report_coverage import advance, covers
from open_energy_planner.appliances.cycles.service import ApplianceService, profile
from tests.app.appliance.raw_support import follow_raw_trace, raw_trace
from tests.app.appliance.support import ENTITY, POWER, readings


@pytest.mark.parametrize("seconds,usable", [(599, True), (600, True), (601, False)])
def test_identical_power_reports_have_an_inclusive_ten_minute_limit(seconds, usable):
    """An unchanged 1-kW value can stay covered through new reports, while an eleven-minute gap cannot."""
    body = raw_trace()
    body["power_states"][-1]["state"] = "1000"
    body["observer_session"] = "one-session"
    first = instant(body["end"])
    coverage = advance(None, body, continuous=False)
    body["end"] = (first + timedelta(seconds=seconds)).isoformat()
    body["power_reported_at"] = body["end"]
    coverage = advance(coverage, body, continuous=True)
    assert covers(coverage, first, instant(body["end"])) is usable


def test_repeated_old_report_does_not_extend_the_observation_period():
    """Polling a held high value never invents new reports or fresh covered seconds."""
    body = raw_trace()
    body["observer_session"] = "one-session"
    body["power_states"][-1]["state"] = "383"
    first = instant(body["end"])
    coverage = advance(None, body, continuous=False)
    for seconds in (300, 600, 601):
        body["end"] = (first + timedelta(seconds=seconds)).isoformat()
        coverage = advance(coverage, body, continuous=True)
        assert not covers(coverage, first, instant(body["end"]))
    assert coverage["valid"] is False


def test_new_ha_session_cannot_bridge_old_coverage():
    """A reloaded integration starts observing again even if the value and report remain recent."""
    body = raw_trace()
    body["observer_session"] = "before-reload"
    first = instant(body["end"])
    coverage = advance(None, body, continuous=False)
    body.update(observer_session="after-reload", end=(first + timedelta(minutes=5)).isoformat())
    body["power_reported_at"] = body["end"]
    coverage = advance(coverage, body, continuous=True)
    assert not covers(coverage, first, instant(body["end"]))


def test_historical_raw_shape_is_visible_but_cannot_claim_live_observation(service):
    """Perfect historical counters and power do not prove report freshness between saved events."""
    body = raw_trace()
    service.learn_raw_history(body, instant(body["end"]))
    settings = service.state["devices"][ENTITY]
    assert "profile" not in settings
    assert settings["raw_history"]["status"] == "not_observed"
    assert max(row["power_w"] or 0 for row in settings["raw_history"]["power_curve"]) == 3000


@pytest.mark.parametrize("interruption", [None, "restart", "gap"])
def test_followed_cycle_survives_restart_only_after_complete_admission(service, interruption):
    """Live unchanged-value reports admit a true pulse, while mid-cycle restart or missing captures prevents refinement."""
    body = raw_trace()
    started = instant(body["power_states"][1]["at"])
    service = follow_raw_trace(
        service,
        body,
        restart_at=started + timedelta(minutes=10) if interruption == "restart" else None,
        gap_at=(started + timedelta(minutes=5), started + timedelta(minutes=20))
        if interruption == "gap"
        else None,
    )
    settings = service.state["devices"][ENTITY]
    if interruption:
        assert "profile" not in settings
        assert settings["raw_history"]["status"] == "not_observed"
    else:
        assert profile(settings["profile"]).energy_kwh == pytest.approx(23 / 45)
        assert max(settings["profile"]["power_w"]) == 3000
        restored = ApplianceService(service.documents, service.consumers)
        assert restored.document() == json.loads(json.dumps(service.document()))


def test_five_minute_bootstrap_uses_mean_power_instead_of_quantized_counter_peaks(service):
    """Twelve active 1-kW intervals with one1-kWh counter step retain1-kW phases instead of a12-kW spike."""
    rows = readings(cycles=1)
    for row in rows:
        if row["power_w"] > 0:
            row["power_w"] = 1000
    # The fixture has twelve active intervals separated by an internal pause.
    active = sum(row["power_w"] > 0 for row in rows)
    total_energy = active / 12
    for index, row in enumerate(rows):
        row["kwh"] = 10 + (total_energy if index >= 21 else 0)
    now = instant(rows[-1]["at"]) + timedelta(minutes=5)
    service.learn_history(ENTITY, POWER, rows, False, now)
    settings = service.state["devices"][ENTITY]
    assert max(settings["learned_profile"]["power_w"]) == pytest.approx(1000)
    assert profile(settings["learned_profile"]).energy_kwh == pytest.approx(total_energy)
    assert settings["profile_basis"] == "statistics"


def test_bad_later_capture_and_statistics_never_degrade_a_followed_profile(service):
    """Once admitted, an observed shape survives a conflicting capture, new statistics and restart."""
    body = raw_trace()
    service = follow_raw_trace(service, body)
    original = deepcopy(service.state["devices"][ENTITY]["profile"])
    conflict = raw_trace(conflict=True)
    service.learn_raw_history(conflict, instant(conflict["end"]))
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, instant(body["end"]))
    assert service.state["devices"][ENTITY]["profile"] == original
    restored = ApplianceService(service.documents, service.consumers)
    assert profile(restored.state["devices"][ENTITY]["profile"]) == profile(original)


def test_followed_learning_preserves_distinct_explicit_completion_contributions(service):
    """The new statistical trust boundary does not delete explicitly recorded completed-cycle evidence."""
    from tests.app.appliance.support import explicit_three_kwh

    explicit_three_kwh(service)
    service = follow_raw_trace(service, raw_trace())
    learned = profile(service.state["devices"][ENTITY]["profile"])
    assert learned.samples == 2
    assert learned.energy_kwh == pytest.approx((3 + 23 / 45) / 2)


def test_new_statistical_shape_replaces_legacy_counter_spike_without_deleting_evidence(service):
    """A prior12-kW counter-derived profile becomes1-kW mean phases while its original archive survives restart."""
    rows = readings(cycles=1)
    for index, row in enumerate(rows):
        row["power_w"] = 1000 if row["power_w"] else 0
        row["kwh"] = 11 if index >= 21 else 10
    started, completed = instant(rows[12]["at"]), instant(rows[27]["at"])
    legacy = {
        "id": "legacy",
        "entity_id": ENTITY,
        "power_entity": POWER,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "synthetic": False,
        "energy_kwh": 1,
        "points": [
            {"at": (instant(row["at"]) + timedelta(minutes=5)).isoformat(), "kwh": row["kwh"]}
            for row in rows[11:27]
        ],
    }
    legacy_profile = {
        "power_w": [0] * 9 + [12000] + [0] * 5,
        "samples": 1,
        "evidence": "measured",
        "phase_seconds": 300,
        "measured_duration_seconds": None,
        "phase_durations_seconds": None,
    }
    service.documents.write("appliance-detected-legacy.json", legacy)
    service.commit(
        {
            "devices": {
                ENTITY: {
                    "shiftable": False,
                    "learning_source": POWER,
                    "profile": legacy_profile,
                    "learned_profile": legacy_profile,
                }
            },
            "tasks": {},
        }
    )
    service.learn_history(
        ENTITY, POWER, rows, False, instant(rows[-1]["at"]) + timedelta(minutes=5)
    )
    assert service.documents.read("appliance-detected-legacy.json") == legacy
    restored = ApplianceService(service.documents, service.consumers)
    restored.learn_history(ENTITY, POWER, [], False, instant(rows[-1]["at"]) + timedelta(minutes=5))
    learned = profile(restored.state["devices"][ENTITY]["profile"])
    assert learned.samples == 1
    assert learned.energy_kwh == pytest.approx(1)
    assert max(learned.power_w) == pytest.approx(1000)
    assert len(list(restored.documents.iter_prefix("appliance-detected-"))) == 2
