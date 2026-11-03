"""Browser validation of the developer entry point without launching VS Code."""

from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture(params=[{"width": 1280, "height": 900}, {"width": 390, "height": 844}])
def page(documentation_url, request, browser):
    """Open the module-scoped static site in a fresh context of the shared browser.

    Use the requested desktop/mobile viewport and touch setting, wait for the
    launch control to become enabled, and close the context after the case.
    """
    with browser.new_context(
        viewport=request.param, has_touch=request.param["width"] < 500
    ) as context:
        page = context.new_page()
        page.goto(documentation_url + "provisioning/")
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
    """Decode the displayed local-clone command without opening an external application."""
    redirect = page.get_by_role("link", name="Clone in VS Code").get_attribute("href")
    command = parse_qs(urlsplit(redirect).query)["url"][0]
    assert command.startswith("vscode://vscode.git/clone?")
    return parse_qs(urlsplit(command).query)


def test_default_launch_and_responsive_content(page):
    """Load the default page on desktop/mobile. Require the right repository, English content and usable overflow-free layout."""
    assert repository_from_link(page) == {
        "url": ["https://github.com/patrickvorgers/open-energy-planner"]
    }
    assert page.locator("html").get_attribute("lang") == "en"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_text("If the button does not open VS Code").click()
    assert page.get_by_text("Git: Clone", exact=True).is_visible()
    assert page.get_by_role("link", name="package preparation").get_attribute("href") == (
        "https://github.com/patrickvorgers/open-energy-planner/blob/main/"
        "docs/development/DEVELOPER-ENVIRONMENT.md#start-and-stop"
    )
    assert page.get_by_text(
        "python -m script.supervisor.dev prepare "
        "--bundle /path/to/installation-bundle-amd64.zip --run-id RUN_ID",
        exact=True,
    ).is_visible()
    assert page.get_by_text("python -m script.supervisor.dev start", exact=True).is_visible()
    assert page.get_by_role("link", name="the local OEP test installation").get_attribute(
        "href"
    ) == ("http://localhost:7123/app/local_open_energy_planner")
    assert page.get_by_text(".local/dev/developer.json", exact=True).is_visible()
    page.screenshot(
        path=f"results/ci/developer-start-{page.viewport_size['width']}.png", full_page=True
    )


def test_branch_url_and_recovery_from_invalid_input(page):
    """Enter branch, invalid and corrected URLs. Preserve valid branch targeting and recover the launch button after correction."""
    field = page.get_by_label("GitHub repository or branch URL")
    field.fill("https://github.com/someone/open-energy-planner/tree/feature/new-flow")
    assert repository_from_link(page) == {
        "url": ["https://github.com/someone/open-energy-planner"],
        "ref": ["feature/new-flow"],
    }
    field.fill("https://github.com/someone/project?token=not-a-real-token")
    assert page.get_by_role("alert").is_visible()
    assert page.locator("#launch").get_attribute("href") is None
    field.fill("https://github.com/someone/project.git")
    assert repository_from_link(page) == {"url": ["https://github.com/someone/project"]}
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
