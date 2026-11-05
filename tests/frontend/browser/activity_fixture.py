"""Synthetic activity evidence through the same publishers used by real background work."""

from datetime import UTC, datetime, timedelta

from aiohttp import web

from open_energy_planner.activities.api import EVENTS
from open_energy_planner.activities.events import Health, Outcome


def register(app):
    """Install fixture-only triggers before freezing the router. Production has no event-write API."""

    async def live(_request):
        """Publish a real same-process transition while the browser is subscribed."""
        events = app[EVENTS]
        identity = await events.start("weather", "weather", "Live weather refresh")
        await events.finish(
            identity, Outcome.COMPLETED, "A usable weather forecast was received.", usable=True
        )
        return web.json_response({"accepted": True})

    async def history_state(request):
        """Publish synthetic persistence health transitions through the real event subscription."""
        events = app[EVENTS]
        events.history_available = (await request.json())["available"]
        events.publish("history", {"available": events.history_available, "lost_updates": 0})
        return web.json_response({"accepted": True})

    app.router.add_post("/_test/activity", live)
    app.router.add_post("/_test/activity-history", history_state)


async def seed(app):
    """Provide more than one page, a recovered outage and current planned work without provider traffic."""
    events = app[EVENTS]
    now = datetime.now(UTC)
    seed_time = now.replace(hour=12, minute=0, second=0, microsecond=0)
    try:
        for index in range(104):
            events.clock = lambda index=index: seed_time - timedelta(minutes=110 - index)
            identity = await events.start("prices:nord_pool", "prices", "Fetch published prices")
            await events.finish(
                identity,
                Outcome.COMPLETED,
                "Adapter: Nord Pool. Market: NL. Delivery-day prices accepted.",
                usable=True,
            )
        events.clock = lambda: seed_time - timedelta(minutes=3)
        identity = await events.start("weather", "weather", "Fetch weather")
        await events.finish(
            identity,
            Outcome.FAILED,
            "The provider request failed. Calendar forecasting remains available.",
        )
        events.clock = lambda: seed_time - timedelta(minutes=2)
        identity = await events.start("weather", "weather", "Fetch weather")
        await events.finish(identity, Outcome.COMPLETED, "Usable weather received.", usable=True)
        events.clock = lambda: seed_time - timedelta(minutes=1)
        await events.start("models", "training", "Update forecast model")
    finally:
        events.clock = lambda: datetime.now(UTC)
    events.availability("weather", Health.AVAILABLE, "Current weather is usable.")
    events.availability("models", Health.UNKNOWN, "Waiting for the current training outcome.")
    events.scheduled("weather", "weather", "Fetch weather", now + timedelta(minutes=55))
