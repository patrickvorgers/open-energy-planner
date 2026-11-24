"""Expose bounded operation durations through standard HTTP diagnostics, never request data."""

from contextlib import asynccontextmanager, contextmanager
from time import perf_counter

from aiohttp import web

REQUEST_STARTED = web.RequestKey("oep-request-started", float)
REQUEST_PHASES = web.RequestKey("oep-request-phases", list)
MILLISECONDS_PER_SECOND = 1000


@contextmanager
def phase(request, name):
    """Measure one app-owned phase, including any awaited worker admission and execution.

    Names are fixed literals supplied by application code. Never use request
    paths, query values, entity identifiers or exception text as a metric name.
    """
    started = perf_counter()
    try:
        yield
    finally:
        request.setdefault(REQUEST_PHASES, []).append(
            (name, (perf_counter() - started) * MILLISECONDS_PER_SECOND)
        )


@asynccontextmanager
async def timed_lock(request, name, lock):
    """Record acquisition latency separately from work performed while holding the lock."""
    with phase(request, name):
        await lock.acquire()
    try:
        yield
    finally:
        lock.release()


def register(app):
    """Measure dispatch through response preparation without persistence or background sampling."""

    @web.middleware
    async def started(request, handler):
        """Start after event-loop dispatch, before authentication and route handling."""
        request[REQUEST_STARTED] = perf_counter()
        return await handler(request)

    async def prepared(request, response):
        """Publish duration-only metadata for successful and failed responses before headers are sent."""
        began = request.get(REQUEST_STARTED)
        if began is not None:
            durations = [
                ("app", (perf_counter() - began) * MILLISECONDS_PER_SECOND),
                *request.get(REQUEST_PHASES, ()),
            ]
            response.headers["Server-Timing"] = ", ".join(
                f"{name};dur={duration:.3f}" for name, duration in durations
            )

    app.middlewares.insert(0, started)
    app.on_response_prepare.append(prepared)
