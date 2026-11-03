"""Provide shared pairing credentials and an isolated loopback HTTP/TLS listener for HA contract tests."""

from contextlib import asynccontextmanager

from aiohttp import web

TOKEN = "local-pairing-admin-only-0123456789"

SECRET = "request-owned-secret-0123456789"

PEER = {"peer_id": "home-assistant-test-12345", "name": "Test Home Assistant", "secret": SECRET}


@asynccontextmanager
async def server(app, context=None):
    """Own one real loopback HTTP/TLS listener with an OS-assigned port."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    try:
        yield f"{'https' if context else 'http'}://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()
