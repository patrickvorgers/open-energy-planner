"""Public session reads share concurrent work without caching settled metadata or credentials."""

import asyncio

import pytest
from aiohttp import web

from tests.frontend.browser import conftest as browser_fixtures


@pytest.fixture(autouse=True)
def concurrent_session_transport(monkeypatch):
    """Keep public metadata in flight briefly so independently loaded documents can join it."""
    create_app = browser_fixtures.create_app

    def create(*arguments, **keywords):
        """Retain the real authenticated app and add latency only to public metadata."""
        app = create_app(*arguments, **keywords)

        @web.middleware
        async def delay(request, handler):
            """Allow sibling callers to reach the same pending public read before its response."""
            if request.path == "/setup/session":
                await asyncio.sleep(0.050)
            return await handler(request)

        app.middlewares.insert(0, delay)
        return app

    monkeypatch.setattr(browser_fixtures, "create_app", create)


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_five_pages_and_workspace_share_only_concurrent_session_reads(app_page):
    """Five real pages and the nested workspace make one public read each, with no authorization header."""
    page, url = app_page
    reads = []
    page.on(
        "request",
        lambda request: reads.append(request) if request.url.endswith("/setup/session") else None,
    )
    for path in (
        "/overview",
        "/setup",
        "/contract",
        "/quality",
        "/activity",
        "/workspace?page=%2Foverview",
    ):
        reads.clear()
        page.goto(url + path)
        page.wait_for_load_state("networkidle")
        assert len(reads) == 1, path
        assert "authorization" not in reads[0].headers


def test_one_cancelled_session_consumer_does_not_cancel_other_readers(app_page):
    """A cancelled waiter leaves the shared bounded read usable, and a later read receives fresh metadata."""
    page, url = app_page
    page.goto(url + "/activity")
    page.wait_for_load_state("networkidle")
    held = []

    def hold(route):
        """Observe actual intercepted reads before asserting the number of shared requests."""
        held.append(route)
        page.evaluate("count => {window.sessionHeldCount = count;}", len(held))

    page.route("**/setup/session", hold)
    page.evaluate("""async () => {
        const script = [...document.scripts].find(script => script.src.endsWith('/activity.js'));
        const {sessionResponse} = await import(new URL('./settings-actions.js', script.src));
        const base = document.querySelector('meta[name="oep-api-base"]').content;
        window.sessionController = new AbortController();
        window.sessionWaiters = {};
        window.freshSessionRead = () => sessionResponse(base).then(response => response.json());
        sessionResponse(base, window.sessionController.signal).then(() => window.sessionWaiters.cancelled = false, () => window.sessionWaiters.cancelled = true);
        window.freshSessionRead().then(value => window.sessionWaiters.survivor = value);
    }""")
    page.wait_for_function("() => window.sessionHeldCount === 1")
    page.evaluate("() => window.sessionController.abort()")
    page.wait_for_function("() => window.sessionWaiters.cancelled === true")
    assert len(held) == 1
    held[0].fulfill(json={"timezone": "UTC", "ingress": False})
    page.wait_for_function("() => window.sessionWaiters.survivor?.timezone === 'UTC'")
    page.evaluate(
        "() => { window.freshSessionRead().then(value => window.sessionWaiters.fresh = value); }"
    )
    page.wait_for_function("() => window.sessionHeldCount === 2")
    assert len(held) == 2
    held[1].fulfill(json={"timezone": "Europe/Berlin", "ingress": False})
    page.wait_for_function("() => window.sessionWaiters.fresh?.timezone === 'Europe/Berlin'")
