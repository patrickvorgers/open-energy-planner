"""Keep Activity browser evidence on its installation day across UTC and DST boundaries."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.activities.api import register
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.frontend.browser import activity_fixture


@pytest.mark.parametrize(
    ("instant", "day", "start", "end"),
    [
        (
            "2026-11-05T22:30:00+00:00",
            "2026-11-05",
            "2026-11-04T23:00:00+00:00",
            "2026-11-05T23:00:00+00:00",
        ),
        (
            "2026-11-05T23:30:00+00:00",
            "2026-11-06",
            "2026-11-05T23:00:00+00:00",
            "2026-11-06T23:00:00+00:00",
        ),
        (
            "2026-11-06T00:30:00+00:00",
            "2026-11-06",
            "2026-11-05T23:00:00+00:00",
            "2026-11-06T23:00:00+00:00",
        ),
        (
            "2026-06-30T22:30:00+00:00",
            "2026-07-01",
            "2026-06-30T22:00:00+00:00",
            "2026-07-01T22:00:00+00:00",
        ),
        (
            "2026-03-28T23:30:00+00:00",
            "2026-03-29",
            "2026-03-28T23:00:00+00:00",
            "2026-03-29T22:00:00+00:00",
        ),
        (
            "2026-10-24T22:30:00+00:00",
            "2026-10-25",
            "2026-10-24T22:00:00+00:00",
            "2026-10-25T23:00:00+00:00",
        ),
    ],
)
def test_activity_fixture_history_matches_installation_day(
    tmp_path, monkeypatch, instant, day, start, end
):
    """All 107 records remain pageable on HA's current day, including 23-hour and 25-hour days."""
    now = datetime.fromisoformat(instant)

    class Clock(datetime):
        """Control only the fixture's captured instant, leaving real event storage and API date rules intact."""

        @classmethod
        def now(cls, tz=None):
            """Return the independent boundary instant without depending on the host timezone."""
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(activity_fixture, "datetime", Clock)

    async def scenario():
        """Publish the real synthetic records and read both pages through the actual activity route."""
        repository = SqlPlanRepository(tmp_path)
        app = web.Application()
        events = register(
            app,
            repository.engine,
            SimpleNamespace(document=lambda: {"notifications": []}),
            lambda: "Europe/Amsterdam",
        )
        events.clock = lambda: now
        try:
            async with TestClient(TestServer(app)) as client:
                await activity_fixture.seed(app)
                await events.flush()
                response = await client.get("/v1/activities", params={"day": day})
                assert response.status == 200
                first = await response.json()
                assert first["day"] == day and first["timezone"] == "Europe/Amsterdam"
                assert len(first["activities"]) == 100
                second = await (
                    await client.get("/v1/activities", params={"day": day, "before": first["next"]})
                ).json()
                assert len(second["activities"]) == 7 and second["next"] is None
                rows = first["activities"] + second["activities"]
                assert len({row["id"] for row in rows}) == 107
                assert all(start <= row["started_at"] < end for row in rows)
                assert events.clock() == now
        finally:
            repository.close()

    asyncio.run(scenario())
