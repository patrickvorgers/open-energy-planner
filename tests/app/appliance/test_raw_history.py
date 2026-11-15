"""Original state events refine only independently conserved, quiet-bracketed completed cycles."""

from copy import deepcopy
from datetime import timedelta

import pytest

from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.cycles.raw import analyze
from open_energy_planner.appliances.cycles.service import ApplianceService, profile
from tests.app.appliance.raw_support import follow_raw_trace, raw_trace, start_observing
from tests.app.appliance.support import ENTITY, POWER


def test_async_counter_anchors_preserve_real_twenty_second_peak_and_energy():
    """Asynchronous anchors conserve 23/45 kWh without moving/scaling a measured 3-kW pulse."""
    body = raw_trace()
    result = analyze(body)
    [cycle] = result["cycles"]
    assert cycle["profile"].duration_seconds == 1800
    assert cycle["profile"].phase_durations_seconds == pytest.approx((600.3, 20, 1179.7))
    assert cycle["profile"].power_w == (1000, 3000, 1000)
    assert cycle["profile"].energy_kwh == pytest.approx(23 / 45)
    assert result["status"] == "complete"
    assert body["power_states"][2]["state"] == "3000"


def test_running_raw_evidence_is_visible_but_never_a_completed_training_sample():
    """A fifteen-minute partial run retains its exact measured start and pulse, without training."""
    result = analyze(raw_trace(completed=False))
    assert not result["cycles"]
    assert result["running"]["status"] == "observed"
    assert result["running"]["energy_kwh"] == pytest.approx(0.25 + 1 / 90)
    assert result["status"] == "running"


@pytest.mark.parametrize("fault", ["conflict", "unknown", "reset", "missing_anchor"])
def test_unprovable_energy_keeps_original_power_curve_without_training(fault):
    """Conflict, unknown/reset or missing anchor preserves the actual 3-kW sample with no fabricated energy."""
    body = raw_trace(conflict=fault == "conflict")
    if fault == "unknown":
        body["energy_states"][1]["state"] = "unavailable"
    elif fault == "reset":
        body["energy_states"][2]["state"] = "9"
    elif fault == "missing_anchor":
        body["energy_states"] = body["energy_states"][1:]
    result = analyze(body)
    assert not result["cycles"]
    assert result["status"] == "unverified"
    assert max(point["power_w"] or 0 for point in result["power_curve"]) == 3000


def test_limited_tail_cannot_invent_missing_quiet_predecessor():
    """A row-capped tail starting in an active run shows actual points without inventing its start."""
    body = raw_trace()
    body["power_states"] = body["power_states"][2:]
    body["limited"] = True
    result = analyze(body)
    assert not result["cycles"] and result["running"] is None


def test_raw_profile_and_original_frame_survive_restart_without_retraining(service):
    """The exact irregular learned profile and original frame survive restart, empty statistics and retry."""
    body = raw_trace()
    start_observing(service, body)
    service.learn_raw_history(body, instant(body["end"]))
    learned = service.state["devices"][ENTITY]["profile"]
    assert profile(learned).energy_kwh == pytest.approx(23 / 45)
    assert profile(learned).phase_boundaries == pytest.approx((0, 600.3, 620.3, 1800))
    service = ApplianceService(service.documents, service.consumers)
    service.learn_raw_history(body, instant(body["end"]))
    service.learn_history(ENTITY, POWER, [], False, instant(body["end"]))
    assert profile(service.state["devices"][ENTITY]["profile"]) == profile(learned)
    assert service.state["devices"][ENTITY]["profile"]["samples"] == 1
    [record] = list(service.documents.iter_prefix("appliance-raw-"))
    assert service.documents.read(record["history_ref"]) == body
    assert record["training_usable"] is True


def test_raw_conflict_remains_visible_and_keeps_existing_statistics_profile(service):
    """A contradictory counter cannot scale away a real 3-kW peak or replace an existing usable profile."""
    from tests.app.appliance.support import NOW, readings

    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    previous = profile(service.state["devices"][ENTITY]["profile"])
    body = raw_trace(conflict=True)
    service.learn_raw_history(body, instant(body["end"]))
    settings = service.state["devices"][ENTITY]
    assert profile(settings["profile"]) == previous
    assert settings["raw_history"]["status"] == "unverified"
    assert max(row["power_w"] or 0 for row in settings["raw_history"]["power_curve"]) == 3000
    [record] = list(service.documents.iter_prefix("appliance-raw-"))
    assert record["training_usable"] is False
    assert service.documents.read(record["history_ref"]) == body


def test_raw_refines_same_five_minute_cycle_without_adding_a_second_sample(service):
    """Statistical and raw views retain both originals but select one 23/45-kWh cycle with its true peak."""
    body = raw_trace()
    start, total, rows = instant(body["power_states"][1]["at"]), 10, []
    for index, watts in enumerate([0] * 7 + [46000 / 45] * 6 + [0] * 6):
        total += watts / 12000
        rows.append(
            {
                "at": (start - timedelta(minutes=35) + timedelta(minutes=5 * index)).isoformat(),
                "power_w": watts,
                "kwh": total,
            }
        )
    service.learn_history(ENTITY, POWER, rows, False, instant(body["end"]))
    assert len(list(service.documents.iter_prefix("appliance-detected-"))) == 1
    start_observing(service, body)
    service.learn_raw_history(body, instant(body["end"]))
    assert service.state["devices"][ENTITY]["profile"]["samples"] == 1
    assert max(service.state["devices"][ENTITY]["profile"]["power_w"]) == 3000
    restored = ApplianceService(service.documents, service.consumers)
    restored.learn_history(ENTITY, POWER, rows, False, instant(body["end"]))
    assert restored.state["devices"][ENTITY]["profile"]["samples"] == 1
    assert len(list(service.documents.iter_prefix("appliance-detected-"))) == 1
    assert len(list(service.documents.iter_prefix("appliance-raw-"))) == 1


def test_partial_raw_run_intercepts_readiness_and_completion_never_rewrites_frozen_task(service):
    """Current raw evidence fixes the measured start without training or creating authenticated execution feedback."""
    from open_energy_planner.appliances.cycles.observed import work
    from tests.app.appliance.support import readings

    body = raw_trace(completed=False)
    start, now = instant(body["power_states"][1]["at"]), instant(body["end"])
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, start - timedelta(hours=1))
    service.configure(ENTITY, True)
    task = service.submit(
        ENTITY,
        "before-manual-start",
        start - timedelta(hours=1),
        start + timedelta(hours=3),
        start - timedelta(hours=1),
    )
    frozen = deepcopy(task["profile"])
    start_observing(service, body)
    service.learn_raw_history(body, now)
    assert service.state["devices"][ENTITY]["profile"]["samples"] == 1
    run = service.observed(ENTITY)
    assert run["started_at"] == start.isoformat()
    assert work(ENTITY, run, now).fixed_start == start
    assert work(ENTITY, run, now).profile.energy(
        start, now, now + timedelta(hours=1)
    ) == pytest.approx(0.25)
    assert service.state["tasks"][ENTITY]["status"] == "execution_observed"
    assert service.state["tasks"][ENTITY]["attempt"] is None
    assert service.state["tasks"][ENTITY]["events"] == []
    complete = raw_trace()
    follow_raw_trace(service, complete)
    assert service.state["devices"][ENTITY]["observed_run"]["status"] == "complete"
    assert service.state["tasks"][ENTITY]["profile"] == frozen
    assert service.state["tasks"][ENTITY]["status"] == "execution_observed"


def test_failed_raw_commit_rolls_back_originals_model_and_cache_then_retries(service, monkeypatch):
    """Failure after frame insertion leaves neither orphan raw evidence nor a published derived cache."""
    body = raw_trace()
    start_observing(service, body)
    previous, cached, write = (
        deepcopy(service.state),
        deepcopy(service._cycle_records),
        service.documents.write,
    )

    def fail(name, value):
        """Reject the final model pointer within the existing atomic archive transaction."""
        if name == "appliance-state.json":
            raise OSError("Synthetic raw archive outage")
        write(name, value)

    monkeypatch.setattr(service.documents, "write", fail)
    with pytest.raises(OSError):
        service.learn_raw_history(body, instant(body["end"]))
    assert service.state == previous and service._cycle_records == cached
    assert not list(service.documents.iter_prefix("appliance-raw-"))
    assert not list(service.documents.iter_prefix("appliance-state-history-"))
    monkeypatch.setattr(service.documents, "write", write)
    service.learn_raw_history(body, instant(body["end"]))
    assert service.state["devices"][ENTITY]["profile"]["samples"] == 1


def test_raw_missing_retention_keeps_model_and_old_source_cannot_publish_late_result(service):
    """Absent raw states retain the last usable model, while changed sensor ownership rejects a late old read."""
    body = raw_trace()
    start_observing(service, body)
    service.learn_raw_history(body, instant(body["end"]))
    previous = profile(service.state["devices"][ENTITY]["profile"])
    empty = {**body, "power_states": [], "energy_states": []}
    service.learn_raw_history(empty, instant(body["end"]))
    assert profile(service.state["devices"][ENTITY]["profile"]) == previous
    service.consumers.options = (
        {"entity_id": ENTITY, "name": "Washer", "power_entity": "sensor.changed_power"},
    )
    service.sync_sources()
    assert "raw_history" not in service.state["devices"][ENTITY]
    assert "profile" not in service.state["devices"][ENTITY]
    with pytest.raises(ValueError, match="current Home Assistant"):
        service.learn_raw_history(body, instant(body["end"]))


def test_union_complexity_rejection_retains_profile_curve_and_does_not_poison_next_refresh(
    service, monkeypatch
):
    """A bounded raw-phase union can reject one new training view while retaining its real curve and frozen work."""
    from open_energy_planner.appliances.cycles import model
    from tests.app.appliance.support import readings

    body = raw_trace()
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, instant(body["end"]))
    previous = profile(service.state["devices"][ENTITY]["profile"])
    start_observing(service, body)
    monkeypatch.setattr(model, "MAX_VARIABLE_PROFILE_STEPS", 4)
    service.learn_raw_history(body, instant(body["end"]))
    assert profile(service.state["devices"][ENTITY]["profile"]) == previous
    assert service.state["devices"][ENTITY]["raw_history"]["status"] == "complexity_limited"

    assert (
        max(
            row["power_w"] or 0
            for row in service.state["devices"][ENTITY]["raw_history"]["power_curve"]
        )
        == 3000
    )
    service.learn_raw_history(body, instant(body["end"]))
    service.learn_history(ENTITY, POWER, [], False, instant(body["end"]))
    assert profile(service.state["devices"][ENTITY]["profile"]) == previous
    assert service.state["devices"][ENTITY]["raw_history"]["status"] == "complexity_limited"


def test_selected_detail_bounds_many_device_curves_and_hides_old_source(service):
    """Sixty maximal retained curves cannot inflate the shared response or expose a replaced sensor's curve."""
    import json

    body = raw_trace()
    service.learn_raw_history(body, instant(body["end"]))
    original = deepcopy(service.state["devices"][ENTITY])
    entities = [f"sensor.raw_energy_{index}" for index in range(60)]
    service.consumers.sync(
        {
            "entities": [{"entity_id": entity, "name": entity} for entity in entities],
            "suggestions": {
                "consumers": [
                    {"entity_id": entity, "upstream": None, "power_entity": POWER}
                    for entity in entities
                ]
            },
        }
    )
    service.consumers.save(
        [{"entity_id": entity, "weather_sensitive": False} for entity in entities],
        service.consumers.version,
    )
    data = deepcopy(service.state)
    curve = [{"at": body["end"], "power_w": 3000} for _ in range(2049)]
    for entity in entities:
        data["devices"][entity] = deepcopy(original)
        data["devices"][entity]["raw_history"].update(
            power_curve=curve, energy_curve=[{"at": row["at"], "kwh": 10} for row in curve]
        )
    service.commit(data)
    summary = service.document()
    assert len(summary["devices"]) == 60
    assert all("power_curve" not in device["raw_history"] for device in summary["devices"])
    assert len(json.dumps(summary).encode()) < 4 * 1024 * 1024
    detail = service.document(raw_entity=entities[0])
    assert len(detail["devices"]) == 1
    assert len(detail["devices"][0]["raw_history"]["power_curve"]) == 2049
    assert len(json.dumps(detail).encode()) < 4 * 1024 * 1024
    service.consumers.options = tuple(
        {**row, "power_entity": "sensor.replacement_power"} for row in service.consumers.options
    )
    assert "raw_history" not in service.document(raw_entity=entities[0])["devices"][0]


def test_global_phase_budget_counts_retained_models_and_frozen_tasks(service):
    """Duplicated learned, effective, observed and frozen shapes share admission rather than exhausting the wire budget."""
    from dataclasses import asdict

    from open_energy_planner.appliances.cycles.model import CycleProfile
    from tests.app.appliance.support import NOW, readings

    body = raw_trace()
    dense = asdict(CycleProfile((1000,) * 2048, phase_durations_seconds=(1,) * 2048))
    other = "sensor.dense_other_energy"
    service.consumers.sync(
        {
            "entities": [{"entity_id": entity, "name": entity} for entity in (ENTITY, other)],
            "suggestions": {
                "consumers": [
                    {"entity_id": entity, "upstream": None, "power_entity": POWER}
                    for entity in (ENTITY, other)
                ]
            },
        }
    )
    service.consumers.save(
        [{"entity_id": entity, "weather_sensitive": False} for entity in (ENTITY, other)],
        service.consumers.version,
    )
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, instant(body["end"]))
    data = deepcopy(service.state)
    data["devices"][ENTITY] = {
        "shiftable": True,
        "profile": dense,
        "learned_profile": dense,
        "observed_run": {
            "profile": dense,
            "status": "complete",
            "power_entity": POWER,
            "observed_until": NOW.isoformat(),
        },
    }
    data["devices"][other] = {"shiftable": False, "profile": dense, "learned_profile": dense}
    service.commit(data)
    service.submit(ENTITY, "frozen-dense", NOW, NOW + timedelta(hours=4), NOW)
    frozen = deepcopy(service.state["tasks"][ENTITY]["profile"])
    start_observing(service, body)
    service.learn_raw_history(body, instant(body["end"]))
    assert service.state["devices"][ENTITY]["raw_history"]["status"] == "complexity_limited"
    assert service.state["tasks"][ENTITY]["profile"] == frozen
    [record] = list(service.documents.iter_prefix("appliance-raw-"))
    assert record["training_usable"] is False
