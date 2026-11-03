"""Exercise the compiled public wiki beneath the actual GitHub Pages project prefix."""

import shutil
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import pytest
from playwright.sync_api import expect


@pytest.fixture(scope="module")
def documentation_url():
    """Serve only the prebuilt publication artifact at its production project prefix."""
    artifact = Path("_site").resolve()
    assert (artifact / "index.html").is_file(), "Build docs/site/mkdocs.yml before browser tests"
    with TemporaryDirectory(dir="results/ci") as directory:
        shutil.copytree(artifact, Path(directory) / "open-energy-planner")
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=directory)
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/open-energy-planner/"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


@pytest.fixture(params=[1280, 768, 390])
def documentation_page(documentation_url, browser, request):
    """Open the real artifact in isolated desktop, tablet and phone browser contexts."""
    with browser.new_context(
        viewport={"width": request.param, "height": 900}, has_touch=request.param < 1000
    ) as context:
        page = context.new_page()
        page.goto(documentation_url)
        yield page


def test_root_article_search_and_provisioning(documentation_page, documentation_url):
    """Keep redirects, search, nested articles and the existing provisioning entry functional."""
    page = documentation_page
    expect(page.get_by_role("heading", name="Your energy, understood.")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.evaluate("document.fonts.check('16px Outfit')")
    page.screenshot(
        path=f"results/ci/documentation-home-{page.viewport_size['width']}.png", full_page=True
    )
    if page.viewport_size["width"] < 1000:
        page.get_by_role("button", name="Menu", exact=True).click()
    search = page.get_by_role("searchbox", name="Search the guide")
    expect(search).to_be_enabled()
    search.fill("first plan")
    page.get_by_role("button", name="Search", exact=True).click()
    result = (
        page.locator("#search-results")
        .get_by_role("link", name="Try your first plan", exact=True)
        .first
    )
    expect(result).to_be_visible()
    result.click()
    expect(page.get_by_role("heading", name="Try your first plan")).to_be_visible()
    page.reload()
    expect(page.get_by_role("heading", name="Try your first plan")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.goto(documentation_url + "provisioning/")
    expect(page.get_by_role("link", name="Clone in VS Code", exact=True)).to_have_attribute(
        "aria-disabled", "false"
    )
    assert (
        page.get_by_role("link", name="Clone in VS Code")
        .get_attribute("href")
        .startswith("https://vscode.dev/redirect?")
    )


def test_articles_remain_readable_without_javascript(documentation_url, browser):
    """Read a direct nested article and follow navigation when optional search is unavailable."""
    with browser.new_context(java_script_enabled=False) as context:
        page = context.new_page()
        response = page.goto(documentation_url + "docs/users/first-plan/")
        assert response.status == 200
        expect(page.get_by_role("heading", name="Try your first plan")).to_be_visible()
        page.get_by_role("navigation", name="Documentation", exact=True).get_by_role(
            "link", name="Before you start", exact=True
        ).click()
        expect(page.get_by_role("heading", name="Before you start")).to_be_visible()
