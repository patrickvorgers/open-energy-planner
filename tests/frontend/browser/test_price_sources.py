"""Published price coverage stays distinct from rolling forecast preferences."""

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

ASSETS = Path(__file__).resolve().parents[3] / "src/open_energy_planner/web"


def test_inactive_saved_source_survives_other_bucket_save_until_explicitly_replaced(price_page):
    """An unavailable stored source stays outside the choices and survives an unrelated save."""
    page = price_page
    page.evaluate("""() => {
        window.requests = [];
        window.snapshot = {
            market_area: 'BE',
            source_preferences: {
                available_sources: ['energy_price_forecast_eu', 'epex_predictor'],
                source_horizons_hours: {energy_price_forecast_eu: 48, epex_predictor: 72},
                preferences: [null, null, null], stored_preferences: ['jeroen_nl', null, null],
                configuration_version: 'saved-with-jeroen'
            },
            forecast_selection: [0, 24, 48].map(start => ({lead_start_hours: start, lead_end_hours: start + 24}))
        };
        window.saveDraft = () => window.sources.save(async (path, method, body) => {
            window.requests.push({path, method, ...body});
            window.snapshot.source_preferences = {...window.snapshot.source_preferences,
                preferences: body.preferences.map(source => ['energy_price_forecast_eu', 'epex_predictor'].includes(source) ? source : null),
                stored_preferences: body.preferences, configuration_version: 'revision-' + window.requests.length};
            return window.snapshot;
        });
        window.sources.update(window.snapshot);
    }""")
    first = page.get_by_label("Forecast preference: 0–24 hours ahead", exact=True)
    second = page.get_by_label("Forecast preference: 24–48 hours ahead", exact=True)
    expect(first.locator('option[value="jeroen_nl"]')).to_have_count(0)
    expect(first.locator("option:checked")).to_have_text("Automatic: Keep saved preference")
    assert not page.evaluate("window.sources.dirty")
    second.select_option("epex_predictor")
    page.evaluate("window.sources.update({...window.snapshot, source_status: []})")
    page.evaluate("window.saveDraft()")
    assert page.evaluate("window.requests[0]") == {
        "path": "/v1/prices/preferences",
        "method": "PUT",
        "preferences": ["jeroen_nl", "epex_predictor", None],
        "configuration_version": "saved-with-jeroen",
        "market_area": "BE",
    }
    assert not page.evaluate("window.sources.dirty")
    expect(first.locator("option:checked")).to_have_text("Automatic: Keep saved preference")
    first.select_option("")
    assert page.evaluate("window.sources.dirty")
    page.evaluate("window.saveDraft()")
    assert page.evaluate("window.requests[1].preferences") == [None, "epex_predictor", None]
    assert page.evaluate("window.requests[1].configuration_version") == "revision-1"
    expect(first.locator("option")).to_have_text(
        ["Automatic", "EnergyPriceForecast EU", "EpexPredictor"]
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_market_switch_removes_unsupported_sources_but_outage_keeps_choices(price_page):
    """Market capabilities rebuild options while a temporary outage retains a manual preference."""
    page = price_page
    page.evaluate("""() => {
        window.snapshot = {
            market_area: 'NL',
            source_preferences: {
                available_sources: ['energy_price_forecast_eu', 'epex_predictor', 'jeroen_nl'],
                source_horizons_hours: {energy_price_forecast_eu: 48, epex_predictor: 72, jeroen_nl: 72},
                preferences: ['jeroen_nl', null, null], configuration_version: 'nl-revision'
            },
            forecast_selection: [0, 24, 48].map(start => ({lead_start_hours: start, lead_end_hours: start + 24}))
        };
        window.sources.update(window.snapshot);
    }""")
    control = page.get_by_label("Forecast preference: 0–24 hours ahead", exact=True)
    expect(control).to_have_value("jeroen_nl")
    page.evaluate(
        "window.sources.update({...window.snapshot, source_status: [], issues: {jeroen_nl: 'temporarily_unavailable'}})"
    )
    expect(control).to_have_value("jeroen_nl")
    expect(control.locator('option[value="jeroen_nl"]')).to_have_count(1)
    page.evaluate("""() => window.sources.update({...window.snapshot, market_area: 'NO1',
        source_preferences: {available_sources: ['energy_price_forecast_eu'],
            source_horizons_hours: {energy_price_forecast_eu: 48},
            preferences: [null, null, null], configuration_version: null}})""")
    expect(control.locator("option")).to_have_text(["Automatic", "EnergyPriceForecast EU"])
    expect(control).to_have_value("")
    tail = page.get_by_label("Forecast preference: 48–72 hours ahead", exact=True)
    assert tail.locator('option[value="energy_price_forecast_eu"]').evaluate(
        "option => option.disabled"
    )
    page.evaluate(
        "window.sources.update({market_area: 'HU', source_preferences: {available_sources: []}})"
    )
    expect(page.locator("#price-source-controls select")).to_have_count(0)


@pytest.fixture(
    params=[
        {"width": 1440, "height": 1000},
        {"width": 768, "height": 1024},
        {"width": 390, "height": 844},
    ]
)
def price_page(browser, request):
    """Render the real source panel and module with an installation timezone unlike the browser."""
    context = browser.new_context(
        viewport=request.param, timezone_id="America/New_York", locale="en-US"
    )
    page = context.new_page()
    html = (ASSETS / "pages/setup.html").read_text()
    panel = html[
        html.index('<section id="price-sources"') : html.index(
            '<nav id="installation-wizard-actions"'
        )
    ]
    panel = panel.replace('id="price-sources" hidden', 'id="price-sources"')

    def serve(route):
        """Serve the real panel, theme and presentation modules without an app or provider."""
        filename = urlsplit(route.request.url).path.rsplit("/", 1)[-1]
        if filename.endswith(".ttf"):
            route.fulfill(path=ASSETS / "public/fonts" / filename, content_type="font/ttf")
        elif filename in (
            "price-sources.js",
            "display.js",
            "style.css",
            "workspace.css",
            "settings-layout.css",
            "installation-layout.css",
        ):
            route.fulfill(
                path=ASSETS / "public" / filename,
                content_type="text/css" if filename.endswith(".css") else "text/javascript",
            )
        else:
            route.fulfill(
                body='<meta name="viewport" content="width=device-width,initial-scale=1">'
                + "".join(
                    f'<link rel="stylesheet" href="/{stylesheet}.css">'
                    for stylesheet in (
                        "style",
                        "workspace",
                        "settings-layout",
                        "installation-layout",
                    )
                )
                + '<body class="workspace settings-workspace installation-workspace"><main>'
                + '<form id="wizard">'
                + panel
                + "</form></main>",
                content_type="text/html",
            )

    page.route("http://price-sources.test/**", serve)
    page.goto("http://price-sources.test/")
    page.evaluate(
        """async () => {
            const {useHomeAssistantTimezone} = await import('/display.js');
            useHomeAssistantTimezone('Europe/Amsterdam');
            const {PriceSourceSettings} = await import('/price-sources.js');
            window.sources = new PriceSourceSettings(() => {});
        }"""
    )
    try:
        yield page
    finally:
        context.close()


@pytest.mark.parametrize("published_hours", [14, 34, 0])
def test_published_basis_uses_contiguous_coverage_and_preserves_forecast_drafts(
    price_page, published_hours
):
    """Known coverage may end before or after 24h and must stop at a forecast or missing interval."""
    page = price_page
    page.evaluate(
        """hours => {
            const start = Date.parse('2026-10-27T09:00:00Z');
            const end = new Date(start + hours * 3600000).toISOString();
            window.snapshot = {
                market_area: 'NL',
                entries: [
                    ...(hours ? [{start: new Date(start).toISOString(), end,
                        kind: 'published', source_id: 'nord_pool'}] : []),
                    {start: end, end: '2026-10-29T09:00:00Z',
                        kind: 'forecast', source_id: 'epex_predictor'},
                ],
                source_preferences: {available_sources: ['epex_predictor', 'jeroen_nl'],
                    preferences: [null], configuration_version: 'fixture'},
                forecast_selection: [{lead_start_hours: 0, lead_end_hours: 24,
                    published_prices_used: hours > 0, active_sources: ['epex_predictor']}],
            };
            window.sources.update(window.snapshot);
        }""",
        published_hours,
    )
    expect(page.get_by_role("heading", name="Nord Pool: Fixed price basis")).to_be_visible()
    expect(page.locator("#price-horizon")).to_contain_text(
        "hours ahead from now, not calendar days"
    )
    expect(page.locator("#price-horizon")).to_contain_text(
        "only to predictions for unpublished hours"
    )
    coverage = page.locator("#price-published-coverage")
    if published_hours:
        expected_end = "10/28/2026, 12:00 AM" if published_hours == 14 else "10/28/2026, 8:00 PM"
        expect(coverage).to_have_text(
            f"Known prices from now until {expected_end}. Forecasts fill only unpublished hours."
        )
    else:
        expect(coverage).to_have_text(
            "No published Nord Pool prices available from now. Forecasts fill unpublished hours."
        )
    control = page.get_by_label("Forecast preference: 0–24 hours ahead", exact=True)
    expect(control.locator("option")).to_have_text(["Automatic", "EpexPredictor", "Jeroen.nl"])
    control.select_option("jeroen_nl")
    control.focus()
    page.evaluate("window.sources.update({...window.snapshot, entries: []})")
    expect(coverage).to_contain_text("No published Nord Pool prices available from now")
    expect(control).to_have_value("jeroen_nl")
    expect(control).to_be_focused()
    page.evaluate(
        """() => {
            window.sources.update({...window.snapshot, entries: [
                {start: '2026-10-27T09:00:00Z', end: '2026-10-27T10:00:00Z',
                    kind: 'published', source_id: 'nord_pool'},
                {start: '2026-10-27T11:00:00Z', end: '2026-10-28T23:00:00Z',
                    kind: 'published', source_id: 'nord_pool'},
            ]});
        }"""
    )
    expect(coverage).to_have_text(
        "Known prices from now until 10/27/2026, 11:00 AM. Forecasts fill only unpublished hours."
    )
    page.evaluate(
        """() => {
            document.getElementById('price-published-coverage').textContent =
                'Published Nord Pool coverage unavailable. Retrying automatically.';
            document.getElementById('price-source-list').replaceChildren();
            window.sources.discard();
        }"""
    )
    expect(coverage).to_have_text(
        "Published Nord Pool coverage unavailable. Retrying automatically."
    )
    expect(page.locator("#price-source-list li")).to_have_count(0)
    expect(control).to_have_value("")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_forecast_availability_keeps_provenance_honest_and_details_with_source(price_page):
    """Unknown issue times stay unknown and open details follow source identity across polling."""
    page = price_page
    expect(page.locator("#price-source-availability")).not_to_have_attribute("open", "")
    page.get_by_text("Forecast availability", exact=True).click()
    page.evaluate(
        """() => {
            window.snapshot = {source_status: [
                {source_id: 'energy_price_forecast_eu', available: true,
                    issued_at: '2026-10-27T17:54:00Z', received_at: '2026-10-27T17:56:00Z',
                    covered_until: '2026-10-29T18:00:00Z'},
                {source_id: 'epex_predictor', available: true, issued_at: null,
                    received_at: '2026-10-27T17:56:00Z', covered_until: '2026-10-30T17:00:00Z'},
                {source_id: '<img src=x onerror=alert(1)>', available: false,
                    issued_at: null, received_at: null, covered_until: null}
            ], issues: {jeroen_nl: '<script>provider error</script>'}};
            window.sources.update(window.snapshot);
        }"""
    )
    overview = page.locator("#price-source-provenance")
    expect(page.get_by_text("Forecast availability", exact=True)).to_be_visible()
    expect(overview).to_contain_text("Actual use is shown in the horizon blocks below")
    rows = overview.locator("li")
    expect(rows).to_have_count(3)
    expect(rows.nth(0).locator("dd")).to_have_text(["10/27/2026, 6:54 PM", "10/29/2026, 7:00 PM"])
    expect(rows.nth(1).locator("dd").first).to_have_text("Not known")
    expect(rows.nth(2)).to_contain_text("Unavailable")
    expect(overview.locator("img, script")).to_have_count(0)
    expect(overview).to_contain_text("<script>provider error</script>")
    detail = overview.get_by_label("Details: EpexPredictor", exact=True)
    detail.focus()
    detail.press("Enter")
    expect(rows.nth(1).locator("details")).to_have_attribute("open", "")
    expect(rows.nth(1)).to_contain_text("Received: 10/27/2026, 6:56 PM")
    expect(rows.nth(1)).to_contain_text("No forecast issue time is available from this source")
    page.evaluate(
        "window.sources.update({...window.snapshot, source_status: [...window.snapshot.source_status].reverse()})"
    )
    expect(overview.locator('details[data-source="epex_predictor"]')).to_have_attribute("open", "")
    expect(
        overview.locator('details[data-source="energy_price_forecast_eu"]')
    ).not_to_have_attribute("open", "")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(
        path=f"results/ci/source-overview-{page.viewport_size['width']}.png", full_page=True
    )
    page.evaluate("window.sources.update({source_status: []})")
    expect(overview).to_contain_text("No forecast source status available")
    expect(overview.locator("li")).to_have_count(0)


def test_configured_forecast_without_accepted_series_stays_visible(price_page):
    """A failed configured provider remains unavailable even before its first accepted forecast."""
    page = price_page
    page.get_by_text("Forecast availability", exact=True).click()
    page.evaluate(
        """window.sources.update({source_preferences: {
            available_sources: ['epex_predictor', 'jeroen_nl']},
            source_status: [{source_id: 'epex_predictor', available: true}],
            issues: {jeroen_nl: 'No usable forecast received'}})"""
    )
    overview = page.locator("#price-source-provenance")
    expect(overview.locator("li")).to_have_count(2)
    failed = overview.locator("li").filter(has_text="Jeroen.nl")
    expect(failed.locator("small")).to_have_text("Unavailable")
    expect(failed.locator("dd")).to_have_text(["Not known", "Not known"])
    failed.get_by_label("Details: Jeroen.nl", exact=True).click()
    expect(failed).to_contain_text("Received: Not known")
    expect(overview).to_contain_text("Jeroen.nl: No usable forecast received")
    expect(overview).not_to_contain_text("No forecast source status available")
    page.evaluate("window.sources.update({source_preferences: {available_sources: ['jeroen_nl']}})")
    expect(overview.locator("li")).to_have_count(1)
    expect(overview.locator("li small")).to_have_text("Unavailable")


def test_automatic_published_route_explains_selection_and_waiting(price_page):
    """Show automatic routes and delays on every viewport, hiding status when no route exists."""
    page = price_page
    status = page.locator("#price-published-source")
    page.evaluate("window.sources.update({})")
    expect(status).to_be_hidden()
    page.evaluate(
        "window.sources.update({published_source: {mode: 'automatic', selected: 'home_assistant_nord_pool', reason: 'ready'}})"
    )
    expect(status).to_contain_text("Automatic: Home Assistant Nord Pool")
    expect(status).to_be_visible()
    page.evaluate(
        "window.sources.update({published_source: {mode: 'automatic', selected: 'home_assistant_nord_pool', reason: 'waiting_for_publication'}})"
    )
    expect(status).to_contain_text("Waiting for Home Assistant to receive published prices")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(
        path=f"results/ci/ha-price-source-{page.viewport_size['width']}.png", full_page=True
    )
    page.evaluate(
        "window.sources.update({published_source: {mode: 'automatic', selected: 'nord_pool_direct', reason: 'not_configured'}})"
    )
    expect(status).to_contain_text(
        "Nord Pool directly: No Home Assistant Nord Pool integration configured"
    )
    page.evaluate("window.sources.update({})")
    expect(status).to_be_hidden()
