"""Exercise the compiled public wiki beneath the actual GitHub Pages project prefix."""

import pytest
from playwright.sync_api import expect


@pytest.fixture(params=[1280, 768, 390])
def documentation_page(documentation_url, browser, request):
    """Follow the root redirect before yielding an isolated desktop, tablet or phone page."""
    with browser.new_context(
        viewport={"width": request.param, "height": 900}, has_touch=request.param < 1000
    ) as context:
        page = context.new_page()
        page.goto(documentation_url)
        expect(page.get_by_role("heading", name="Your energy, understood.")).to_be_visible()
        yield page


def test_root_article_search_and_provisioning(documentation_page, documentation_url):
    """Keep redirects, search, nested articles and the existing provisioning entry functional."""
    page = documentation_page
    assert page.url == documentation_url + "docs/"
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

    page.get_by_role("link", name="host interpreter setup", exact=True).click()
    expect(page).to_have_url(documentation_url + "docs/development/#first-start")
    expect(page.get_by_role("heading", name="First start")).to_be_visible()


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


def test_user_journey_and_developer_entry_have_readable_figures(
    documentation_page, documentation_url
):
    """Follow audience routes and load informative figures without overflow at all three sizes."""
    page = documentation_page
    navigation = page.get_by_role("navigation", name="Documentation", exact=True)
    if page.viewport_size["width"] < 801:
        page.get_by_role("button", name="Menu", exact=True).click()
    navigation.get_by_role("link", name="How OEP works", exact=True).click()
    expect(page.get_by_role("heading", name="How OEP works")).to_be_visible()
    for route in (
        "installation",
        "contract",
        "forecasts",
        "appliances",
        "read-your-plan",
        "data-storage",
    ):
        page.goto(documentation_url + f"docs/users/{route}/")
        expect(page.locator("main h1")).to_be_visible()
        figures = page.locator("main img")
        assert figures.count() > 0
        for figure in figures.all():
            expect(figure).to_have_js_property("complete", True)
            assert figure.evaluate("image => image.naturalWidth > 0 && image.naturalHeight > 0")
            assert figure.get_attribute("alt")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.goto(documentation_url + "docs/development/")
    page.get_by_role("link", name="reference library", exact=True).first.click()
    expect(page.get_by_role("heading", name="Developer reference library")).to_be_visible()
    page.get_by_role("link", name="Architecture overview", exact=True).last.click()
    expect(page.get_by_role("heading", name="Architecture overview")).to_be_visible()


def test_wiki_and_provisioning_share_the_oep_identity(documentation_page, documentation_url):
    """Both deployed routes load the OEP fonts, palette, logo and keyboard-visible controls."""
    page = documentation_page
    expected = {
        "background": "rgb(245, 251, 255)",
        "ink": "rgb(25, 38, 63)",
        "action": "rgb(0, 109, 166)",
        "bodyFont": "Outfit",
        "headingFont": "Space Grotesk",
        "fieldBorder": "rgb(84, 101, 122)",
    }
    for route, action in (("docs/", ".search-form button"), ("provisioning/", "#launch")):
        page.goto(documentation_url + route)
        page.mouse.move(0, 0)
        page.evaluate("document.fonts.ready")
        appearance = page.evaluate(
            """action => ({
                background: getComputedStyle(document.documentElement).backgroundColor,
                ink: getComputedStyle(document.body).color,
                fieldBorder: getComputedStyle(document.querySelector('input')).borderTopColor,
                action: getComputedStyle(document.querySelector(action)).backgroundColor,
                bodyFont: getComputedStyle(document.body).fontFamily.split(',')[0].replaceAll('"', ''),
                headingFont: getComputedStyle(document.querySelector('h1')).fontFamily.split(',')[0].replaceAll('"', '')
            })""",
            action,
        )
        assert appearance == expected
        assert page.evaluate("document.fonts.check('17px Outfit')")
        assert page.evaluate("document.fonts.check('32px \"Space Grotesk\"')")
        logo = page.locator(".site-brand img")
        expect(logo).to_have_js_property("complete", True)
        assert logo.evaluate("image => image.naturalWidth > 0")
        assert logo.evaluate("image => image.currentSrc") == documentation_url + "assets/app.svg"
    launch = page.get_by_role("link", name="Clone in VS Code", exact=True)
    expect(launch).to_have_attribute("aria-disabled", "false")
    assert (
        launch.bounding_box()["y"] + launch.bounding_box()["height"] <= page.viewport_size["height"]
    )
    page.get_by_label("GitHub repository or branch URL").press("Tab")
    expect(launch).to_be_focused()
    expect(launch).to_have_css("outline-width", "3px")
    page.screenshot(
        path=f"results/ci/provisioning-shared-style-{page.viewport_size['width']}.png",
        full_page=True,
    )


def test_search_submission_closes_menu_before_worker_results(documentation_url, browser):
    """Submitting pending mobile search uncovers the later results without another menu toggle."""
    with browser.new_context(viewport={"width": 390, "height": 844}) as context:
        page = context.new_page()
        page.add_init_script("""
            const RealWorker = window.Worker;
            window.Worker = class extends RealWorker {
                addEventListener(type, listener, ...options) {
                    super.addEventListener(type, event => {
                        if (event.data.results) {
                            window.deliverSearchResults = () => listener(event);
                        } else {
                            listener(event);
                        }
                    }, ...options);
                }
            };
        """)
        page.goto(documentation_url)
        menu = page.get_by_role("button", name="Menu", exact=True)
        menu.click()
        search = page.get_by_role("searchbox", name="Search the guide")
        expect(search).to_be_enabled()
        search.fill("first plan")
        page.get_by_role("button", name="Search", exact=True).click()
        expect(menu).to_have_attribute("aria-expanded", "false")
        expect(page.locator("#search-panel")).to_be_hidden()
        page.wait_for_function("typeof window.deliverSearchResults === 'function'")
        page.evaluate("window.deliverSearchResults()")
        expect(page.locator("#search-panel")).to_be_visible()
        expect(
            page.locator("#search-results")
            .get_by_role("link", name="Try your first plan", exact=True)
            .first
        ).to_be_visible()
        page.screenshot(path="results/ci/documentation-search-pending-390.png", full_page=True)
