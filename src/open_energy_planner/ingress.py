"""Trust only the configured Supervisor socket peer, never forwarded client-address headers."""

import re

from aiohttp import web

INGRESS_PEER = web.AppKey("ingress_peer", str)


def prefix(request):
    """Return the validated ingress prefix only for a connection from the trusted proxy."""
    expected = request.app.get(INGRESS_PEER)
    value = request.headers.get("X-Ingress-Path", "").rstrip("/")
    if (
        expected
        and request.remote == expected
        and re.fullmatch(r"/api/hassio_ingress/[A-Za-z0-9_-]+", value)
    ):
        return value
    return None
