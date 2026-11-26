"""Accept advisory Home Assistant lifecycle events independently of command authority."""

from datetime import UTC, datetime, timedelta

from aiohttp import web

from open_energy_planner.notifications.model import NoticeText, Notification, Severity

HINT_LIFETIME = timedelta(minutes=5)
SHUTDOWN_TITLES = {
    "restarting": "Home Assistant is restarting",
    "stopping": "Home Assistant is stopping",
}


class HomeAssistantLifecycle:
    """Remember only a recent, explicitly reported shutdown until Core starts again."""

    def __init__(self, clock=None):
        """Keep the diagnostic hint process-local."""
        self._clock = clock or (lambda: datetime.now(UTC))
        self._shutdown = None

    def register(self, app, mark_command_free):
        """Accept lifecycle hints through the existing authenticated app and peer routes."""

        async def update(request):
            """Change only the lifecycle diagnostic, with no plan or settings mutations."""
            mark_command_free(request)
            try:
                body = await request.json()
            except ValueError:
                raise web.HTTPBadRequest(text="Invalid Home Assistant lifecycle hint") from None
            state = body.get("state") if isinstance(body, dict) else None
            if not isinstance(state, str) or state not in (*SHUTDOWN_TITLES, "started"):
                raise web.HTTPBadRequest(text="Invalid Home Assistant lifecycle hint")
            self._shutdown = None if state == "started" else (state, self._clock())
            return web.json_response({"accepted": True})

        app.router.add_post("/v1/ha/lifecycle", update)

    def notice(self):
        """Expire the inference if there is no fresh startup signal."""
        if self._shutdown is None:
            return None
        state, since = self._shutdown
        if self._clock() >= since + HINT_LIFETIME:
            return None
        return Notification(
            "home-assistant:lifecycle",
            f"home_assistant_{state}",
            NoticeText(
                SHUTDOWN_TITLES[state],
                "Planning waits for current measurements when Home Assistant comes back.",
                level=Severity.INFO,
            ),
            since=since,
        )
