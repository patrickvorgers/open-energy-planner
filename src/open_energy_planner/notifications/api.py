"""Expose authenticated module-owned notices through the existing app middleware."""

from aiohttp import web

from .collection import NotificationCenter

NOTIFICATIONS = web.AppKey("notifications", NotificationCenter)


def register(app: web.Application, center: NotificationCenter):
    """Install one read-only collection route and a shared module-registration seam."""
    app[NOTIFICATIONS] = center

    async def notifications(_request):
        """Read a synchronous status snapshot without taking the product mutation lock."""
        return web.json_response(center.document(), headers={"Cache-Control": "no-store"})

    app.router.add_get("/v1/notifications", notifications)
