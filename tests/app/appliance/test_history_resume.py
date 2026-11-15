"""Interrupted fine backfills and unchanged uploads preserve coverage without rebuilding models."""

from copy import deepcopy
from datetime import timedelta

import pytest

from open_energy_planner.appliances.cycles import history
from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.cycles.service import ApplianceService, profile
from tests.app.appliance.support import (
    ENTITY,
    NOW,
    POWER,
    complete_running_readings,
    explicit_three_kwh,
    next_batch,
    readings,
    running_readings,
    start_fine,
    trace_refinement,
)


@pytest.mark.parametrize("completed", [False, True])
def test_fine_extension_after_twenty_day_outage_visits_gap_and_retains_old_cursor(
    service, completed
):
    """A .5-kWh run outside the latest week is found after both interrupted and completed scans resume."""
    first, old_end = NOW - timedelta(days=40), NOW - timedelta(hours=1)
    start_fine(service, first, old_end, NOW)
    next_batch(service, [], NOW)
    if completed:
        while history.request(service.state["devices"][ENTITY], resolution="5minute"):
            next_batch(service, [], NOW)
    before_cursor = service.state["devices"][ENTITY]["fine_import"]["cursor"]
    later = NOW + timedelta(days=20)
    rows = [
        {**row, "at": (instant(row["at"]) + timedelta(days=8)).isoformat()}
        for row in readings(cycles=1)
    ]
    assert all(old_end < instant(row["at"]) < later - timedelta(days=7) for row in rows)
    service = ApplianceService(service.documents, service.consumers)
    service.learn_history(ENTITY, POWER, [], False, later)
    start_fine(service, first, later, later)
    assert service.state["devices"][ENTITY]["fine_import"]["cursor"] == before_cursor
    visited = 0
    while history.request(service.state["devices"][ENTITY], resolution="5minute"):
        pending = next_batch(service, rows, later)
        previous = deepcopy(service.state)
        history.batch(service, ENTITY, POWER, [], False, **pending, now=later, resolution="5minute")
        assert service.state == previous
        service = ApplianceService(service.documents, service.consumers)
        visited += 1
        assert visited <= 9
    settings = service.state["devices"][ENTITY]
    assert settings["fine_import"]["phase"] == "complete"
    assert settings["profile"]["samples"] == 1
    assert profile(settings["profile"]).energy_kwh == pytest.approx(0.5)
    assert settings["fine_import"]["expected_intervals"] == 60 * 24 * 12
    assert len(list(service.documents.iter_prefix("appliance-detected-"))) == 1


def test_unchanged_recent_uploads_skip_refinement_but_still_process_observed_completion(
    service, monkeypatch
):
    """A warm archive set is not rebuilt for repeats, empty uploads or a new observation without completed evidence."""
    calls = trace_refinement(monkeypatch)
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    assert len(calls) == 1
    for rows in (readings(cycles=1), [], running_readings()):
        service.learn_history(ENTITY, POWER, rows, False, NOW)
    assert len(calls) == 1
    assert service.state["devices"][ENTITY]["observed_run"]["status"] == "observed"
    service.learn_history(
        ENTITY, POWER, complete_running_readings(), False, NOW + timedelta(minutes=90)
    )
    assert len(calls) == 2
    assert service.state["devices"][ENTITY]["observed_run"]["status"] == "complete"
    service = ApplianceService(service.documents, service.consumers)
    service.learn_history(ENTITY, POWER, [], False, NOW + timedelta(minutes=90))
    assert len(calls) == 3
    service.learn_history(ENTITY, POWER, [], False, NOW + timedelta(minutes=90))
    assert len(calls) == 3
    explicit_three_kwh(service)
    assert len(calls) == 4
    service.learn_history(ENTITY, POWER, [], False, NOW + timedelta(minutes=90))
    assert len(calls) == 4


@pytest.mark.parametrize("resolution", ["hour", "5minute"])
def test_empty_batches_advance_without_rebuilding_unchanged_archives(
    service, monkeypatch, resolution
):
    """Warm unchanged evidence is reused while each requested core still commits coverage."""
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    calls = trace_refinement(monkeypatch)
    history.scan(
        service,
        ENTITY,
        POWER,
        "processing",
        (NOW - timedelta(days=20)).isoformat(),
        NOW.isoformat(),
        NOW,
        resolution=resolution,
    )
    next_batch(service, [], NOW, resolution=resolution)
    next_batch(service, [], NOW, resolution=resolution)
    assert calls == []
    field = "fine_import" if resolution == "5minute" else "history_import"
    assert service.state["devices"][ENTITY][field]["expected_intervals"] == 14 * 24 * (
        12 if resolution == "5minute" else 1
    )


def test_fine_extension_and_batch_failures_keep_both_frontiers_and_real_coverage(
    service, monkeypatch
):
    """Repeated target growth, failed writes and stale requests cannot reset or skip either examined range."""
    first = NOW - timedelta(days=40)
    start_fine(service, first, NOW, NOW)
    next_batch(service, [], NOW)
    backward = service.state["devices"][ENTITY]["fine_import"]["cursor"]
    original = service.documents.write

    def fail_progress(name, value):
        """Fail the durable checkpoint before publishing either frontier."""
        if name == "appliance-progress.json":
            raise OSError("Synthetic fine checkpoint outage")
        original(name, value)

    before = deepcopy(service.state)
    monkeypatch.setattr(service.documents, "write", fail_progress)
    later = NOW + timedelta(days=20)
    with pytest.raises(OSError):
        start_fine(service, first, later, later)
    assert service.state == before
    assert ApplianceService(service.documents, service.consumers).state == before
    monkeypatch.setattr(service.documents, "write", original)
    start_fine(service, first, later, later)
    before = deepcopy(service.state)
    monkeypatch.setattr(service.documents, "write", fail_progress)
    with pytest.raises(OSError):
        next_batch(service, [], later)
    assert service.state == before
    assert ApplianceService(service.documents, service.consumers).state == before
    monkeypatch.setattr(service.documents, "write", original)
    next_batch(service, [], later)
    progress = service.state["devices"][ENTITY]["fine_import"]
    assert progress["forward_cursor"] == (NOW + timedelta(days=7)).isoformat()
    start_fine(service, first, later + timedelta(days=1), later + timedelta(days=1))
    progress = service.state["devices"][ENTITY]["fine_import"]
    assert progress["cursor"] == backward
    assert progress["forward_cursor"] == (NOW + timedelta(days=7)).isoformat()
    assert progress["backfill_end"] == NOW.isoformat()
    with pytest.raises(ValueError, match="current Home Assistant"):
        history.batch(
            service,
            ENTITY,
            "sensor.wrong_power",
            [],
            False,
            **history.request(service.state["devices"][ENTITY], resolution="5minute"),
            now=later,
            resolution="5minute",
        )
    while history.request(service.state["devices"][ENTITY], resolution="5minute"):
        next_batch(service, [], later + timedelta(days=1))
    progress = service.state["devices"][ENTITY]["fine_import"]
    assert progress["phase"] == "complete"
    assert progress["expected_intervals"] == 61 * 24 * 12
    assert progress["usable_intervals"] == progress["detected_cycles"] == 0


def test_failed_new_evidence_retries_refinement_without_poisoning_warm_cache(service, monkeypatch):
    """Failed new evidence is rebuilt on retry, then reused after the successful commit."""
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    calls = trace_refinement(monkeypatch)
    original = service.documents.write

    def fail_state(name, value):
        """Reject the model pointer after attempted evidence insertion."""
        if name == "appliance-state.json":
            raise OSError("Synthetic model outage")
        original(name, value)

    monkeypatch.setattr(service.documents, "write", fail_state)
    with pytest.raises(OSError):
        service.learn_history(ENTITY, POWER, readings(cycles=2), False, NOW)
    assert len(calls) == 1
    assert service.state["devices"][ENTITY]["profile"]["samples"] == 1
    monkeypatch.setattr(service.documents, "write", original)
    service.learn_history(ENTITY, POWER, readings(cycles=2), False, NOW)
    assert len(calls) == 2
    assert service.state["devices"][ENTITY]["profile"]["samples"] == 2
    service.learn_history(ENTITY, POWER, readings(cycles=2), False, NOW)
    assert len(calls) == 2


def test_return_to_previously_cached_source_rebuilds_withdrawn_profile(service, monkeypatch):
    """Reusing an earlier source pair still restores its actual evidence after a source change."""
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    calls = trace_refinement(monkeypatch)
    service.consumers.options = ({"entity_id": ENTITY, "power_entity": "sensor.other_power"},)
    service.sync_sources()
    service.learn_history(ENTITY, "sensor.other_power", [], False, NOW)
    service.consumers.options = ({"entity_id": ENTITY, "power_entity": POWER},)
    service.sync_sources()
    service.learn_history(ENTITY, POWER, [], False, NOW)
    assert len(calls) == 2
    assert profile(service.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(0.5)
    service.learn_history(ENTITY, POWER, [], False, NOW)
    assert len(calls) == 2


def test_forward_batch_activity_reports_usable_coverage_and_skips_only_exact_replay(service):
    """Real HTTP activity marks actual paired forward evidence usable and its identical replay skipped."""
    import asyncio
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.activities.api import EVENTS
    from open_energy_planner.activities.events import Events
    from open_energy_planner.appliances.cycles.api import register
    from open_energy_planner.storage.adapters.activities import ActivityStore

    async def scenario():
        """Exercise genuine batch admission, SQL progress and ActivityStore publication."""
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        first, old_end = now - timedelta(days=40), now - timedelta(days=20)
        start_fine(service, first, old_end, now)
        start_fine(service, first, now, now)
        backward = service.state["devices"][ENTITY]["fine_import"]["cursor"]
        pending = history.request(service.state["devices"][ENTITY], resolution="5minute")
        app = web.Application()
        events = Events(ActivityStore(service.documents.engine))
        app[EVENTS] = events
        register(
            app,
            service,
            SimpleNamespace(state={"enabled": False, "plan": None}),
            None,
            asyncio.Lock(),
            SimpleNamespace(state={"points": []}),
            SimpleNamespace(state={"stores": []}),
        )
        core_start = instant(pending["start"])
        body = {
            "entity_id": ENTITY,
            "power_entity": POWER,
            "rows": [
                {"at": (core_start - timedelta(minutes=5)).isoformat(), "power_w": 0, "kwh": 1},
                {"at": core_start.isoformat(), "power_w": 0, "kwh": 1},
            ],
            "synthetic": False,
            **pending,
        }
        try:
            async with TestClient(TestServer(app)) as client:
                for expected_state, usable in (("completed", True), ("skipped", False)):
                    cursor = events.cursor
                    response = await client.post("/v1/appliances/fine_history", json=body)
                    assert response.status == 200
                    assert service.state["devices"][ENTITY]["fine_import"]["cursor"] == backward
                    terminal = [
                        row["value"]
                        for row in events.changes(cursor)["events"]
                        if row["topic"] == "activity" and row["value"]["state"] != "running"
                    ]
                    assert len(terminal) == 1
                    assert terminal[0]["state"] == expected_state
                    assert terminal[0]["usable"] is usable
        finally:
            await events.close()

    asyncio.run(scenario())
