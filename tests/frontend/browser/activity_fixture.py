"""Synthetic activity evidence through the same publishers used by real background work."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

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

    async def burst(_request):
        """Produce enough real terminal updates to exercise bounded browser retention and paging."""
        events = app[EVENTS]
        for activity_index in range(150):
            identity = await events.start("weather", "weather", f"Weather burst {activity_index}")
            await events.finish(
                identity, Outcome.COMPLETED, "Usable synthetic weather.", usable=True
            )
        await events.flush()
        return web.json_response({"accepted": True})

    async def health(request):
        """Change or withdraw one synthetic source through the real live publisher."""
        body = await request.json()
        if body.get("state") is None:
            app[EVENTS].withdraw(body["owner"])
        else:
            app[EVENTS].availability(body["owner"], Health(body["state"]), body["detail"])
        return web.json_response({"accepted": True})

    app.router.add_post("/_test/activity-burst", burst)
    app.router.add_post("/_test/activity", live)
    app.router.add_post("/_test/activity-history", history_state)
    app.router.add_post("/_test/activity-health", health)


async def seed(app):
    """Seed the Amsterdam discovery scenario's current local day, recovery and upcoming work."""
    events = app[EVENTS]
    now = datetime.now(UTC)
    seed_time = (
        now.astimezone(ZoneInfo("Europe/Amsterdam"))
        .replace(hour=12, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )
    try:
        for activity_index in range(104):
            events.clock = lambda activity_index=activity_index: (
                seed_time - timedelta(minutes=110 - activity_index)
            )
            owner, title, provider = "prices:nord_pool", "Fetch published prices", "Nord Pool"
            if activity_index == 103:
                owner, title, provider = "prices:jeroen_nl:NL", "Fetch price forecast", "Jeroen.nl"
            elif activity_index == 102:
                owner, provider = "prices:epex_predictor:NL", "EpexPredictor"
                title = "Fetch price forecast for the next planning horizon and retain previously accepted prices"
            identity = await events.start(owner, "prices", title)
            await events.finish(
                identity,
                Outcome.COMPLETED,
                f"Adapter: {provider}. Market: NL. Price data accepted.",
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
    events.availability("solar-weather", Health.DEGRADED, "Retaining valid solar weather.")
    events.availability("temperature-weather", Health.AVAILABLE, "Current temperature is usable.")
    events.availability("models", Health.UNKNOWN, "Waiting for the current training outcome.")
    events.availability("prices:nord_pool:NL", Health.AVAILABLE, "Published NL prices are usable.")
    events.availability(
        "prices:epex_predictor:NL",
        Health.UNAVAILABLE,
        "NL forecast unavailable. Published prices remain usable.",
    )
    events.availability("prices:jeroen_nl:NL", Health.AVAILABLE, "NL forecast is usable.")
    events.scheduled("weather", "weather", "Fetch weather", now + timedelta(minutes=55))
    events.scheduled("models", "training", "Later model training", now + timedelta(hours=7))
