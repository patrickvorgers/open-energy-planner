"""Browser validation of the developer entry point without launching VS Code."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture(scope="module")
def site():
    """Serve the developer entry page on isolated loopback HTTP and shut down its thread after tests."""
    directory = Path(__file__).resolve().parents[3] / "docs/developer"
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=directory)
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(params=[{"width": 1280, "height": 900}, {"width": 390, "height": 844}])
def page(site, request, browser):
    """Open the module-scoped static site in a fresh context of the shared browser.

    Use the requested desktop/mobile viewport and touch setting, wait for the
    launch control to become enabled, and close the context after the case.
    """
    with browser.new_context(
        viewport=request.param, has_touch=request.param["width"] < 500
    ) as context:
        page = context.new_page()
        page.goto(site)
        page.locator("#launch[aria-disabled=false]").wait_for()
        yield page


def test_browser_cases_start_without_cookies_or_web_storage(page):
    """Both viewport cases start clean, then leave markers that the next case's page fixture must isolate."""
    assert page.context.cookies() == []
    assert page.evaluate("localStorage.getItem('isolation')") is None
    assert page.evaluate("sessionStorage.getItem('isolation')") is None
    page.evaluate("localStorage.setItem('isolation', 'previous')")
    page.evaluate("sessionStorage.setItem('isolation', 'previous')")
    page.context.add_cookies([{"name": "isolation", "value": "previous", "url": page.url}])
    assert page.context.cookies()


def repository_from_link(page):
    """Decode the displayed VS Code clone link and require the expected Dev Containers command."""
    redirect = page.get_by_role("link", name="Open in VS Code").get_attribute("href")
    command = parse_qs(urlsplit(redirect).query)["url"][0]
    assert command.startswith("vscode://ms-vscode-remote.remote-containers/cloneInVolume?")
    return parse_qs(urlsplit(command).query)["url"][0]


def test_default_launch_and_responsive_content(page):
    """Load the default page on desktop/mobile. Require the right repository, English content and usable overflow-free layout."""
    assert repository_from_link(page) == "https://github.com/patrickvorgers/open-energy-planner"
    assert page.locator("html").get_attribute("lang") == "en"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_text("If the button does not open VS Code").click()
    assert page.get_by_text(
        "Dev Containers: Clone Repository in Container Volume...", exact=True
    ).is_visible()


def test_branch_url_and_recovery_from_invalid_input(page):
    """Enter branch, invalid and corrected URLs. Preserve valid branch targeting and recover the launch button after correction."""
    field = page.get_by_label("GitHub repository or branch URL")
    field.fill("https://github.com/someone/open-energy-planner/tree/feature/new-flow")
    assert repository_from_link(page).endswith("/tree/feature/new-flow")
    field.fill("https://github.com/someone/project?token=not-a-real-token")
    assert page.get_by_role("alert").is_visible()
    assert page.locator("#launch").get_attribute("href") is None
    field.fill("https://github.com/someone/project.git")
    assert repository_from_link(page) == "https://github.com/someone/project"
    assert not page.get_by_role("alert").is_visible()


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "https://github.com.attacker.invalid/owner/repo",
        "https://user:password@github.com/owner/repo",
        "https://github.com/owner/repo/issues/1",
    ],
)
def test_rejects_unsafe_or_unsupported_destinations(page, value):
    """Enter a dangerous or unsupported destination. Show validation feedback and disable the launch link."""
    page.get_by_label("GitHub repository or branch URL").fill(value)
    assert page.get_by_role("alert").is_visible()
    assert page.locator("#launch").get_attribute("aria-disabled") == "true"
    assert page.locator("#launch").get_attribute("href") is None
