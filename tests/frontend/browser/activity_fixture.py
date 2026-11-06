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

    async def contexts(_request):
        """Publish independent synthetic subjects and two failed plan valuations without hiding either."""
        events = app[EVENTS]
        start = datetime(2026, 10, 25, 0, 0, 17, tzinfo=UTC)
        end = start + timedelta(hours=2)
        subjects = [
            (
                "demand",
                "training",
                "Update consumption forecast",
                {"label": "Household consumption"},
            ),
            ("solar", "training", "Update solar forecast", {"label": "East roof, West roof"}),
            ("appliances", "history", "Learn appliance cycles", {"label": "Laundry <img src=x>"}),
            ("models", "training", "Compare forecast models", {"label": "East roof"}),
            (
                "forecast-quality",
                "quality",
                "Archive daily forecast",
                {"start": start.isoformat(), "end": end.isoformat()},
            ),
            (
                "planning",
                "planning",
                "Calculate energy plan",
                {"start": start.isoformat(), "end": end.isoformat()},
            ),
            (
                "settlement",
                "settlement",
                "Evaluate measured savings",
                {"start": start.isoformat(), "end": end.isoformat()},
            ),
        ]
        for owner, kind, title, context in subjects:
            if owner == "settlement":
                context.update(
                    waiting_start=(start + timedelta(hours=1)).isoformat(),
                    waiting_end=end.isoformat(),
                )
            identity = await events.start(owner, kind, title, context=context)
            await events.finish(
                identity,
                Outcome.SKIPPED,
                "This attempt processed 1 of 2 measured intervals. Published electricity prices are missing for the indicated interval. The next background check tries again. Previous published financial results remain available."
                if owner == "settlement"
                else "The captured period is being investigated.",
            )
        for offset in (0, 1):
            identity = await events.start(
                "contract-valuations",
                "settlement",
                "Recalculate plan value",
                context={
                    "label": "Archived plan",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "plan_generated_at": (
                        start - timedelta(days=1) + timedelta(hours=10, minutes=offset * 15)
                    ).isoformat(),
                    "plan_strategy": "minimize cost",
                    "plan_id": f"example-plan-{offset}",
                },
            )
            await events.finish(
                identity,
                Outcome.FAILED,
                "Could not update the financial estimate for this archived plan. Its last successful estimate is retained. "
                "This does not change the active energy plan or its device commands. "
                "Open the corresponding plan under Planning to check its cost information and review Contract settings. "
                "If the cause is still unclear, report this activity for investigation.",
            )
        events.scheduled(
            "solar",
            "training",
            "Update solar forecast",
            datetime.now(UTC) + timedelta(minutes=30),
            context={"label": "East roof, West roof"},
        )
        await events.flush()
        return web.json_response({"accepted": True})

    async def plan_link(request):
        """Publish a synthetic correction referencing an independently seeded real archive."""
        events = app[EVENTS]
        identity = await events.start(
            "contract-valuations",
            "settlement",
            "Recalculate plan value",
            context=await request.json(),
        )
        await events.finish(
            identity,
            Outcome.FAILED,
            "The prior estimate is retained. View the original plan to inspect its evidence.",
        )
        await events.flush()
        return web.json_response({"accepted": True})

    app.router.add_post("/_test/activity-burst", burst)
    app.router.add_post("/_test/activity", live)
    app.router.add_post("/_test/activity-history", history_state)
    app.router.add_post("/_test/activity-health", health)
    app.router.add_post("/_test/activity-contexts", contexts)
    app.router.add_post("/_test/activity-plan", plan_link)


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
