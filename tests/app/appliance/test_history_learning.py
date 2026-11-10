"""Independent numerical and restart evidence for recency-weighted historical learning."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.appliances.cycles import history
from open_energy_planner.appliances.cycles.learning import (
    detect_cycles,
    history_evidence,
    hourly_profile,
    retained_weight,
)
from open_energy_planner.appliances.cycles.model import CycleProfile, average_profiles, instant
from open_energy_planner.appliances.cycles.service import ApplianceService
from tests.app.appliance.test_appliance_learning import ENTITY, NOW, POWER, readings


def hourly_rows(start, powers):
    """Pair hourly mean W with cumulative kWh at interval end, starting from a negative origin."""
    total, rows = -50.0, []
    for index, power in enumerate(powers):
        total += power / 1000
        rows.append(
            {"at": (start + timedelta(hours=index)).isoformat(), "power_w": power, "kwh": total}
        )
    return rows


def begin(service, start, end):
    """Initialize the same source-bound range used by the Recorder transport."""
    history.scan(service, ENTITY, POWER, "processing", start.isoformat(), end.isoformat(), NOW)


def upload(service, rows):
    """Send only the currently requested core with the detector's independent thirteen-hour overlap."""
    pending = service.document()["devices"][0]["history_request"]
    first, last = instant(pending["start"]), instant(pending["end"])
    bounded = [
        row
        for row in rows
        if first - timedelta(hours=13) <= instant(row["at"]) < last + timedelta(hours=1)
    ]
    history.batch(service, ENTITY, POWER, bounded, False, **pending, now=NOW)
    return pending


def test_ninety_days_halves_prior_weight_without_changing_raw_count_or_energy():
    """A recent 1-kWh run weighs twice a .5-kWh run ninety days older, giving 5/6 kWh."""
    old = CycleProfile((500,) * 4)
    new = CycleProfile((1000,) * 4)
    weight = retained_weight(1, NOW - timedelta(days=90), NOW)
    result = average_profiles(old, new, previous_weight=weight)
    assert weight == pytest.approx(0.5)
    assert result.samples == 2
    assert result.energy_kwh == pytest.approx(5 / 6)
    assert result.duration_seconds == 3600


@pytest.mark.parametrize("weight", [-1, True, float("nan"), float("inf")])
def test_average_rejects_invalid_retained_weights(weight):
    """Invalid weights cannot make a finite accepted profile invalid or reverse recency."""
    with pytest.raises(ValueError):
        average_profiles(CycleProfile((100,)), CycleProfile((200,)), previous_weight=weight)


def test_hourly_estimate_conserves_energy_and_keeps_full_occupied_hours():
    """Two active hourly bins total .75 kWh and conservatively occupy two full hours."""
    rows = hourly_rows(NOW - timedelta(days=800), [0, 0, 500, 250, 0])
    [(started, completed, points)] = detect_cycles(rows, NOW, sample_seconds=3600)
    learned = hourly_profile(points, started, completed)
    assert learned.evidence == "hourly_estimate"
    assert learned.duration_seconds == 7200
    assert learned.power_w == (500,) * 4 + (250,) * 4
    assert learned.energy_kwh == pytest.approx(0.75)


@pytest.mark.parametrize("fault", ["gap", "reset", "missing_power", "unfinished"])
def test_hourly_faults_do_not_invent_a_complete_cycle(fault):
    """Missing bins, counter resets, missing power and absent closing quiet evidence refuse a run."""
    rows = hourly_rows(NOW - timedelta(days=800), [0, 0, 500, 250, 0])
    if fault == "gap":
        rows.pop(3)
    elif fault == "reset":
        rows[3]["kwh"] -= 2
    elif fault == "missing_power":
        rows[3]["power_w"] = None
    else:
        rows.pop()
    assert detect_cycles(rows, NOW, sample_seconds=3600) == []


def test_hourly_profile_requires_actual_counter_points_and_exact_boundaries():
    """An isolated helper call still refuses missing, nonfinite and mismatched counter evidence."""
    start, end = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    for points in (
        [],
        [{"at": start.isoformat(), "kwh": 1}],
        [{"at": start.isoformat(), "kwh": 1}, {"at": end.isoformat(), "kwh": float("nan")}],
        [
            {"at": (start - timedelta(hours=1)).isoformat(), "kwh": 1},
            {"at": end.isoformat(), "kwh": 2},
        ],
    ):
        with pytest.raises(ValueError):
            hourly_profile(points, start, end)


@pytest.mark.parametrize(
    "field,value",
    [
        ("power_w", 20001),
        pytest.param("power_w", 10**400, id="power-huge-int"),
        ("power_w", -1),
        ("power_w", float("inf")),
        ("power_w", float("nan")),
        ("kwh", 1e12 + 1),
        pytest.param("kwh", 10**400, id="energy-huge-int"),
        ("kwh", -1e12 - 1),
        ("kwh", float("inf")),
        ("kwh", float("nan")),
    ],
)
def test_numeric_sensor_faults_do_not_block_later_fine_cycles(service, field, value):
    """An isolated numeric fault breaks its run, excludes unusable deltas and still admits two later .5-kWh cycles."""
    rows = readings(cycles=3)
    rows[13][field] = value
    service.learn_history(ENTITY, POWER, rows, False, NOW)
    row = service.document()["devices"][0]
    assert row["learned_profile"]["samples"] == 2
    assert sum(row["profile"]["power_w"]) / 4000 == pytest.approx(0.5)
    assert row["learning_history"]["expected_intervals"] == 117
    assert row["learning_history"]["usable_intervals"] == (115 if field == "power_w" else 114)
    assert rows[13][field] is value


@pytest.mark.parametrize(
    "field,value",
    [
        ("power_w", 20001),
        pytest.param("power_w", 10**400, id="power-huge-int"),
        ("power_w", -1),
        ("power_w", float("inf")),
        ("power_w", float("nan")),
        ("kwh", 1e12 + 1),
        pytest.param("kwh", 10**400, id="energy-huge-int"),
        ("kwh", -1e12 - 1),
        ("kwh", float("inf")),
        ("kwh", float("nan")),
    ],
)
def test_numeric_sensor_faults_advance_hourly_checkpoint_without_learning_invalid_run(
    service, field, value
):
    """A faulty first hourly run cannot trap its batch. Two later valid runs learn and exact usable coverage excludes the fault."""
    start = NOW - timedelta(days=800)
    begin(service, start, start + timedelta(hours=9))
    rows = hourly_rows(start, [0, 0, 500, 0, 0, 1000, 0, 0, 500, 0])
    rows[2][field] = value
    upload(service, rows)
    row = service.document()["devices"][0]
    assert row["profile"]["samples"] == 2
    assert row["history_import"]["phase"] == "complete"
    assert row["history_import"]["cursor"] == (start + timedelta(hours=9)).isoformat()
    assert row["history_import"]["usable_intervals"] == (7 if field == "power_w" else 6)
    assert row["history_import"]["detected_cycles"] == 2
    assert rows[2][field] is value


@pytest.mark.parametrize("field", ["power_w", "kwh"])
@pytest.mark.parametrize("value", [True, "missing", []])
def test_malformed_quantity_types_remain_errors(field, value):
    """Unknown numerical measurements are distinct from malformed transport types, which still reject the upload."""
    rows = readings()
    rows[13][field] = value
    with pytest.raises(ValueError, match="quantity type"):
        detect_cycles(rows, NOW)


def test_checkpoint_restart_overlap_and_empty_hours_never_duplicate_coverage(service):
    """A boundary-crossing run is admitted once after restart. Empty core hours count as examined, not usable."""
    start = NOW - timedelta(days=900)
    end = start + timedelta(days=8)
    # Completion on the first core boundary requires its closing quiet bin from the lookahead.
    rows = hourly_rows(start - timedelta(hours=1), [0] * 168 + [500, 0, 0, 1000, 0])
    begin(service, start, end)
    revision = service.revision
    pending = upload(service, rows)
    first = deepcopy(service.state)
    assert service.document()["devices"][0]["history_import"]["cursor"] == pending["end"]
    assert service.document()["devices"][0]["hourly_profile"]["samples"] == 1
    assert "profile" not in service.document()["devices"][0]
    assert service.revision == revision
    history.batch(service, ENTITY, POWER, [], False, **pending, now=NOW)
    assert service.state == first
    restored = ApplianceService(service.documents, service.consumers)
    upload(restored, rows)
    row = restored.document()["devices"][0]
    assert row["history_import"]["phase"] == "complete"
    assert row["history_import"]["expected_intervals"] == 192
    assert row["history_import"]["usable_intervals"] == 172
    assert row["history_import"]["detected_cycles"] == 2
    assert row["profile"]["samples"] == 2
    assert row["profile"]["evidence"] == "hourly_estimate"
    assert row["profile_kind"] == "Historical estimate"
    assert row["learning_status"] == "provisional"
    assert not row["shiftable"] and row["task"] is None
    assert len(list(service.documents.iter_prefix("appliance-hourly-"))) == 2


@pytest.mark.parametrize("prior", ["manual", "fine", "explicit", "none"])
def test_model_precedence_and_frozen_task_survive_learning(service, prior):
    """Manual/fine models win. An admitted provisional task remains frozen when fine learning replaces its model."""
    if prior == "manual":
        service.set_manual_profile(ENTITY, 90, 0.6, service.revision)
    elif prior == "fine":
        service.learn_history(ENTITY, POWER, readings(), False, NOW)
    elif prior == "explicit":
        [(started, completed, points)] = detect_cycles(readings(cycles=1), NOW)
        service.record_cycle(ENTITY, "confirmed-run", started, completed, points)
    start = NOW - timedelta(days=700)
    rows = hourly_rows(start, [0, 0, 500, 0, 0, 1000, 0])
    begin(service, start, start + timedelta(hours=6))
    upload(service, rows)
    row = service.document()["devices"][0]
    assert (
        row["profile_kind"]
        == {
            "manual": "Manual",
            "fine": "Learned",
            "explicit": "Learned",
            "none": "Historical estimate",
        }[prior]
    )
    service.configure(ENTITY, True)
    task = service.submit(ENTITY, "new-ready", NOW, NOW + timedelta(hours=4), NOW)
    service.learn_history(ENTITY, POWER, readings(), False, NOW)
    assert service.document()["devices"][0]["profile_kind"] == "Learned"
    assert service.document()["devices"][0]["task"]["profile"] == task["profile"]


def test_failed_scan_resumes_without_restarting_and_source_change_withdraws_estimate(service):
    """Restart retains cursor and archives. Relinking the Energy power meter refuses stale uploads."""
    start, end = NOW - timedelta(days=600), NOW - timedelta(days=592)
    begin(service, start, end)
    upload(service, [])
    pending = service.document()["devices"][0]["history_request"]
    history.scan(service, ENTITY, POWER, "failed", None, None, NOW)
    restored = ApplianceService(service.documents, service.consumers)
    assert restored.document()["devices"][0]["history_request"] is None
    begin(restored, start, end)
    assert restored.document()["devices"][0]["history_request"] == pending
    restored.consumers.options = (
        {"entity_id": ENTITY, "name": "Washer", "power_entity": "sensor.new_power"},
    )
    restored.sync_sources()
    assert "history_import" not in restored.document()["devices"][0]
    with pytest.raises(ValueError, match="current Home Assistant"):
        history.batch(restored, ENTITY, POWER, [], False, **pending, now=NOW)


def test_legacy_automatic_evidence_reconstructs_weights_before_next_run(service):
    """Retained automatic points allow exact conversion of a legacy equal-weight aggregate."""
    service.learn_history(ENTITY, POWER, readings(), False, NOW)
    legacy = deepcopy(service.state)
    legacy["devices"][ENTITY].pop("learning_weight")
    legacy["devices"][ENTITY].pop("first_detected_start")
    service.commit(legacy)
    service.learn_history(ENTITY, POWER, readings(cycles=3), False, NOW)
    row = service.document()["devices"][0]
    assert row["profile"]["samples"] == 3
    assert 2.9 < row["learning_weight"] < 3
    assert sum(row["profile"]["power_w"]) / 4000 == pytest.approx(0.5)


def test_usable_coverage_counts_missing_core_hours_and_not_overlap():
    """One missing hour counts against six examined hours. Neither lookback nor lookahead inflates coverage."""
    start = NOW - timedelta(days=1000)
    rows = hourly_rows(start - timedelta(hours=1), [0] * 8)
    rows.pop(3)
    result = history_evidence(
        rows, 0, sample_seconds=3600, start=start, end=start + timedelta(hours=6)
    )
    assert result["expected_intervals"] == 6
    assert result["usable_intervals"] == 4


def test_failed_checkpoint_write_rolls_back_profile_and_archive(service, monkeypatch):
    """A failed checkpoint commit must not publish the new model or leave an orphan cycle archive."""
    start = NOW - timedelta(days=800)
    begin(service, start, start + timedelta(hours=6))
    previous, original = deepcopy(service.state), service.documents.write

    def fail_pointer(name, value):
        """Reject the current pointer after cycle and state-version writes within the transaction."""
        if name == "appliance-state.json":
            raise OSError("Fixture database outage")
        original(name, value)

    monkeypatch.setattr(service.documents, "write", fail_pointer)
    with pytest.raises(OSError):
        upload(service, hourly_rows(start, [0, 0, 500, 0, 0, 1000, 0]))
    assert service.state == previous
    assert list(service.documents.iter_prefix("appliance-hourly-")) == []


def test_progress_only_updates_do_not_archive_models_and_failed_progress_stays_resumable(
    service, monkeypatch
):
    """Fine coverage and empty hourly batches update only durable progress, with rollback and restart preserving the last cursor."""
    start = NOW - timedelta(days=800)
    begin(service, start, start + timedelta(days=8))
    previous = deepcopy(service.state)
    snapshots = list(service.documents.iter_prefix("appliance-state-"))
    stored = service.documents.read("appliance-progress.json")
    original = service.documents.write

    def fail_progress(name, value):
        """Reject a progress-only write before any in-memory checkpoint can advance."""
        if name == "appliance-progress.json":
            raise OSError("Fixture progress outage")
        original(name, value)

    monkeypatch.setattr(service.documents, "write", fail_progress)
    with pytest.raises(OSError):
        upload(service, [])
    assert service.state == previous
    assert service.documents.read("appliance-progress.json") == stored
    assert ApplianceService(service.documents, service.consumers).state == previous
    monkeypatch.setattr(service.documents, "write", original)
    upload(service, [])
    service.learn_history(ENTITY, POWER, [], False, NOW)
    service.learn_history(
        ENTITY,
        POWER,
        [
            {"at": (NOW - timedelta(seconds=offset)).isoformat(), "power_w": 0, "kwh": 1}
            for offset in (600, 300)
        ],
        False,
        NOW,
    )
    assert list(service.documents.iter_prefix("appliance-state-")) == snapshots
    assert ApplianceService(service.documents, service.consumers).state == service.state
    saved = service.documents.read("appliance-state.json")["devices"][ENTITY]
    assert "history_import" not in saved and "learning_history" not in saved
    assert (
        service.state["devices"][ENTITY]["history_import"]["cursor"]
        == (start + timedelta(days=7)).isoformat()
    )
    assert service.state["devices"][ENTITY]["learning_history"]["usable_intervals"] == 1


def test_hourly_api_rejects_forged_progress_and_replans_only_on_effective_profile(service):
    """Exact schemas, source ownership and ordered ranges guard uploads. Progress alone does not trigger planning."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.appliances.cycles.api import register

    async def scenario():
        """Exercise real HTTP parsing and SQL commits, without replacing the owning service with a mock."""
        from open_energy_planner.activities.api import EVENTS
        from open_energy_planner.activities.events import Events, Outcome
        from open_energy_planner.storage.adapters.activities import ActivityStore

        app, refreshes = web.Application(), []
        events = Events(ActivityStore(service.documents.engine))
        app[EVENTS] = events
        service.configure(ENTITY, False)
        register(
            app,
            service,
            SimpleNamespace(state={"enabled": False, "plan": None}),
            SimpleNamespace(refresh=lambda **values: refreshes.append(values)),
            asyncio.Lock(),
            SimpleNamespace(state={"points": []}),
            SimpleNamespace(state={"stores": []}),
        )
        start = (datetime.now(UTC) - timedelta(days=30)).replace(minute=0, second=0, microsecond=0)
        body = {
            "entity_id": ENTITY,
            "power_entity": POWER,
            "phase": "processing",
            "start": start.isoformat(),
            "end": (start + timedelta(hours=9)).isoformat(),
        }
        async with TestClient(TestServer(app)) as client:
            for changed in (
                {"phase": "complete"},
                {"power_entity": "sensor.wrong"},
                {"end": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
                {"unexpected": True},
            ):
                response = await client.post(
                    "/v1/appliances/history_scan", json={**body, **changed}
                )
                assert response.status == 400
            response = await client.post("/v1/appliances/history_scan", json=body)
            assert response.status == 200
            assert refreshes == []
            pending = (await response.json())["appliances"]["devices"][0]["history_request"]
            data = {
                "entity_id": ENTITY,
                "power_entity": POWER,
                **pending,
                "rows": hourly_rows(start, [0, 0, 500, 0, 0, 1000, 0, 0, 500, 0]),
                "synthetic": False,
            }
            data["rows"][2]["power_w"] = 20001
            response = await client.post(
                "/v1/appliances/hourly_history",
                json={**data, "start": (start + timedelta(hours=1)).isoformat()},
            )
            assert response.status == 400
            response = await client.post("/v1/appliances/hourly_history", json=data)
            assert response.status == 200
            assert (await response.json())["appliances"]["devices"][0][
                "learning_status"
            ] == "provisional"
            assert len(refreshes) == 1
            failed = await events.start("appliances", "history", "Earlier history request")
            await events.finish(failed, Outcome.FAILED, "Synthetic failed request.")
            cursor = events.cursor
            response = await client.post("/v1/appliances/hourly_history", json=data)
            assert response.status == 200 and len(refreshes) == 1
            terminal = [
                row["value"]
                for row in events.changes(cursor)["events"]
                if row["topic"] == "activity" and row["value"]["state"] != "running"
            ]
            assert len(terminal) == 1
            assert terminal[0]["context"] == {"label": "Washer"}
            assert terminal[0]["state"] == "skipped"
            assert not terminal[0]["usable"] and not terminal[0]["recovered"]
            assert ("appliances", "history") in events.failed
        await events.close()

    asyncio.run(scenario())
