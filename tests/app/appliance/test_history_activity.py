"""History activity distinguishes examined coverage from actual usable paired measurements."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.activities.api import EVENTS
from open_energy_planner.activities.events import Events, Outcome
from open_energy_planner.appliances.cycles import history
from open_energy_planner.appliances.cycles.api import register
from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.storage.adapters.activities import ActivityStore
from tests.app.appliance.support import ENTITY, POWER

CASES = [
    (operation, quality)
    for operation in ("fine_history", "hourly_history", "recent_fine", "recent_hour")
    for quality in ("empty", "baseline", "missing", "invalid", "gap", "reset", "usable")
] + [(operation, "outside_core") for operation in ("fine_history", "hourly_history")]


@pytest.mark.parametrize("operation,quality", CASES)
def test_history_activity_recovers_only_for_real_usable_intervals(service, operation, quality):
    """Fresh empty/faulty reads and batch replays cannot clear a failure episode. Real paired deltas can."""

    async def scenario():
        """Exercise actual HTTP admission, durable progress, domain evidence and ActivityStore events."""
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        resolution = "hour" if operation in ("hourly_history", "recent_hour") else "5minute"
        step = timedelta(seconds=3600 if resolution == "hour" else 300)
        bounded = operation in ("fine_history", "hourly_history")
        pending = {}
        if bounded:
            first, old_end = now - timedelta(days=40), now - timedelta(days=20)
            for end in (old_end, now):
                history.scan(
                    service,
                    ENTITY,
                    POWER,
                    "processing",
                    first.isoformat(),
                    end.isoformat(),
                    now,
                    resolution=resolution,
                )
            pending = history.request(service.state["devices"][ENTITY], resolution=resolution)
            start = instant(pending["start"])
        else:
            start = now - timedelta(days=1)
        rows = [
            {"at": (start - step).isoformat(), "power_w": 0, "kwh": 1},
            {"at": start.isoformat(), "power_w": 0, "kwh": 1},
        ]
        if quality == "empty":
            rows = []
        elif quality == "baseline":
            rows = rows[1:]
        elif quality == "missing":
            rows[1]["kwh"] = None
        elif quality == "invalid":
            rows[1]["power_w"] = 20001
        elif quality == "gap":
            rows[0]["at"] = (start - 2 * step).isoformat()
        elif quality == "reset":
            rows[0]["kwh"] = 2
        elif quality == "outside_core":
            for row in rows:
                row["at"] = (instant(row["at"]) - 2 * step).isoformat()
        events = Events(ActivityStore(service.documents.engine))
        app = web.Application()
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
        failure = await events.start("appliances", "history", "Earlier failed read")
        await events.finish(failure, Outcome.FAILED, "Synthetic history fault.")
        body = {
            "entity_id": ENTITY,
            "power_entity": POWER,
            "rows": rows,
            "synthetic": False,
            **pending,
        }
        if not bounded:
            body["resolution"] = resolution
        endpoint = operation if bounded else "history"
        try:
            async with TestClient(TestServer(app)) as client:
                for replay in range(2 if bounded else 1):
                    cursor = events.cursor
                    response = await client.post("/v1/appliances/" + endpoint, json=body)
                    assert response.status == 200
                    terminal = [
                        row["value"]
                        for row in events.changes(cursor)["events"]
                        if row["topic"] == "activity" and row["value"]["state"] != "running"
                    ]
                    assert len(terminal) == 1
                    usable = quality == "usable" and not replay
                    assert terminal[0]["state"] == ("completed" if usable else "skipped")
                    assert terminal[0]["usable"] is usable
                    assert terminal[0]["recovered"] is usable
                    assert (("appliances", "history") in events.failed) is (quality != "usable")
                    if bounded:
                        progress = service.state["devices"][ENTITY][
                            "fine_import" if resolution == "5minute" else "history_import"
                        ]
                        assert progress["expected_intervals"] == 7 * 24 * (
                            12 if resolution == "5minute" else 1
                        )
                        if replay:
                            assert (
                                terminal[0]["detail"]
                                == "This appliance history batch was already processed."
                            )
                        elif not usable:
                            assert "no usable paired measurements" in terminal[0]["detail"]
                if quality != "usable":
                    recovery = dict(body)
                    if bounded:
                        recovery.update(
                            history.request(service.state["devices"][ENTITY], resolution=resolution)
                        )
                        recovery_start = instant(recovery["start"])
                    else:
                        recovery_start = start
                    recovery["rows"] = [
                        {"at": (recovery_start - step).isoformat(), "power_w": 0, "kwh": 1},
                        {"at": recovery_start.isoformat(), "power_w": 0, "kwh": 1},
                    ]
                    cursor = events.cursor
                    response = await client.post("/v1/appliances/" + endpoint, json=recovery)
                    assert response.status == 200
                    terminal = [
                        row["value"]
                        for row in events.changes(cursor)["events"]
                        if row["topic"] == "activity" and row["value"]["state"] != "running"
                    ]
                    assert len(terminal) == 1
                    assert terminal[0]["state"] == "completed"
                    assert terminal[0]["usable"] is True and terminal[0]["recovered"] is True
                    assert ("appliances", "history") not in events.failed
        finally:
            await events.close()

    asyncio.run(scenario())
