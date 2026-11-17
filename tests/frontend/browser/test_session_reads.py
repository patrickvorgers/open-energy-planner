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


@pytest.mark.parametrize("shell_waiter", [True, False])
def test_discarded_child_session_owner_cannot_strand_shell_or_replacement(app_page, shell_waiter):
    """A discarded child with a nonsettling transport cannot strand shell waiters or subsequent reads."""
    page, url = app_page
    page.goto(url + "/workspace?page=%2Factivity")
    page.wait_for_load_state("networkidle")
    held = []

    def hold(route):
        """Keep the real child fetch pending until its owning document is discarded."""
        held.append(route)
        page.evaluate("count => {window.sessionHeldCount = count;}", len(held))

    page.route("**/setup/session", hold)
    child = page.locator("#workspace-content").content_frame
    child.locator("body").evaluate("""async () => {
        const script = [...document.scripts].find(script => script.src.endsWith('/activity.js'));
        const {sessionResponse} = await import(new URL('./settings-actions.js', script.src));
        const original = window.fetch;
        window.fetch = (...arguments_) => {
            original(...arguments_).catch(() => {});
            return new Promise(() => {});
        };
        sessionResponse(document.querySelector('meta[name="oep-api-base"]').content).catch(() => {});
    }""")
    page.wait_for_function("() => window.sessionHeldCount === 1")
    page.evaluate(
        """async shellWaiter => {
        const script = [...document.scripts].find(script => script.src.endsWith('/workspace.js'));
        const {sessionResponse} = await import(new URL('./settings-actions.js', script.src));
        const base = document.querySelector('meta[name="oep-api-base"]').content;
        window.readFreshSession = () => sessionResponse(base).then(response => response.json());
        window.shellSessionStarted = performance.now();
        if (shellWaiter) window.readFreshSession().then(() => window.shellSessionOutcome = 'success', error => {
            window.shellSessionOutcome = error.name;
            window.shellSessionDuration = performance.now() - window.shellSessionStarted;
        });
        document.getElementById('workspace-content').src = 'about:blank';
    }""",
        shell_waiter,
    )
    if shell_waiter:
        page.wait_for_function("() => window.shellSessionOutcome !== undefined", timeout=6500)
        assert page.evaluate("window.shellSessionOutcome") == "TimeoutError"
        assert page.evaluate("window.shellSessionDuration") <= 6000
    else:
        page.wait_for_function(
            "() => performance.now() - window.shellSessionStarted >= 5100", timeout=6500
        )
    assert len(held) == 1
    page.evaluate("() => {window.readFreshSession().then(value => window.freshSession = value);}")
    page.wait_for_function("() => window.sessionHeldCount === 2", timeout=1500)
    held[1].fulfill(json={"timezone": "Europe/Berlin", "ingress": False})
    page.wait_for_function("() => window.freshSession?.timezone === 'Europe/Berlin'")
