"""Keep EV request retries, private failures and bounded evidence reads independent."""

from datetime import datetime, timedelta

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.ev_request_fixture import configure_ev
from tests.frontend.browser.helpers import TOKEN

PRIVATE_SENTINEL = "PRIVATE_EV_FAILURE_DO_NOT_DISPLAY"


def open_requests(page, url):
    """Open the actual app with synthetic defaults and an editable complete request."""
    headers, _before = configure_ev(page, url)
    page.goto(url + "/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    card = page.locator("#vehicle-controls > article").first
    card.get_by_role("button", name="Plan charging request", exact=True).click()
    card.get_by_label("Target state of charge (%)", exact=True).fill("65")
    expect(page.locator("#refresh-evidence")).to_be_enabled()
    return headers, card


@pytest.mark.parametrize("failure", ["read_response", "write_response", "preparation"])
def test_private_failure_text_never_reaches_visible_ev_diagnostics(app_page, failure):
    """Unrecognized bodies and input-preparation exceptions cannot expose private values as advice."""
    page, url = app_page
    _headers, card = open_requests(page, url)
    if failure == "read_response":
        page.route("**/v1/ev", lambda route: route.fulfill(status=503, body=PRIVATE_SENTINEL))
        with page.expect_response("**/v1/ev"):
            page.locator("#refresh-evidence").click()
        status = page.locator("#status")
        expect(status).not_to_contain_text("Connected:")
    else:
        if failure == "write_response":
            page.route(
                "**/v1/ev/requests",
                lambda route: (
                    route.fulfill(
                        status=400,
                        body="Charging request changed. Refresh before saving: " + PRIVATE_SENTINEL,
                    )
                    if route.request.method == "POST"
                    else route.fallback()
                ),
            )
        else:
            departure = card.get_by_label("Ready by", exact=True)
            departure.fill(
                (datetime.fromisoformat(departure.input_value()) + timedelta(minutes=1)).isoformat(
                    timespec="minutes"
                )
            )
            page.evaluate(
                """privateValue => {
                const original = Intl.DateTimeFormat;
                Intl.DateTimeFormat = new Proxy(original, {construct(target, args) {
                    if (args[0] === 'sv-SE') throw new Error(privateValue);
                    return Reflect.construct(target, args);
                }});
            }""",
                PRIVATE_SENTINEL,
            )
        card.get_by_role("button", name="Plan charging request", exact=True).click()
        status = card.get_by_role("status")
        expect(status).not_to_have_text("")
    expect(status).not_to_contain_text(PRIVATE_SENTINEL)
    assert float(card.get_by_label("Target state of charge (%)", exact=True).input_value()) == 65


def hold_read_body(page, path):
    """Simulate response headers followed by a body that stops only when its actual signal aborts."""
    page.evaluate(
        """path => {
        const original = window.fetch;
        window.originalEvReadFetch = original;
        window.heldReads = [];
        window.fetch = async (input, options = {}) => {
            if (new URL(input, location.href).pathname === path && (options.method || 'GET') === 'GET') {
                window.heldReads.push({hasSignal: Boolean(options.signal), aborted: false});
                const reading = window.heldReads.at(-1);
                return new Response(new ReadableStream({start(controller) {
                    options.signal?.addEventListener('abort', () => {
                        reading.aborted = true;
                        controller.error(new DOMException('Synthetic body cancellation', 'AbortError'));
                    }, {once: true});
                }}), {headers: {'Content-Type': 'application/json'}});
            }
            return original(input, options);
        };
    }""",
        path,
    )


@pytest.mark.parametrize("path", ["/v1/ev", "/v1/ev/requests"])
def test_evidence_deadline_aborts_body_read_and_allows_an_independent_retry(app_page, path):
    """A stuck state/history body reaches its 30-second deadline and releases refresh without losing a draft."""
    page, url = app_page
    _headers, card = open_requests(page, url)
    page.clock.install()
    hold_read_body(page, path)
    page.locator("#refresh-evidence").click()
    page.wait_for_function("() => window.heldReads.length === 1")
    assert page.evaluate("window.heldReads[0].hasSignal")
    page.clock.run_for(31000)
    expect(page.locator("#status")).to_contain_text("could not be read")
    assert page.evaluate("window.heldReads[0].aborted")
    page.evaluate("() => { window.fetch = window.originalEvReadFetch; }")
    expect(page.locator("#refresh-evidence")).to_be_enabled()
    page.locator("#refresh-evidence").click()
    expect(page.locator("#refresh-evidence")).to_be_enabled()
    assert float(card.get_by_label("Target state of charge (%)", exact=True).input_value()) == 65


def test_manual_refresh_does_not_start_overlapping_evidence_reads(app_page):
    """Repeated manual activation shares the current load instead of opening more pending state reads."""
    page, url = app_page
    _headers, _card = open_requests(page, url)
    hold_read_body(page, "/v1/ev")
    page.locator("#refresh-evidence").click()
    page.wait_for_function("() => window.heldReads.length === 1")
    page.locator("#refresh-evidence").evaluate("button => {button.click(); button.click();}")
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    assert page.evaluate("window.heldReads.length") == 1
    expect(page.locator("#refresh-evidence")).to_be_disabled()


def test_uncertain_post_keeps_exact_retry_identity_without_automatic_repetition(app_page):
    """An accepted write with a lost reply remains an uncertain draft and explicit UUID retry creates no duplicate."""
    page, url = app_page
    headers, card = open_requests(page, url)
    page.evaluate(
        """privateValue => {
        const original = window.fetch;
        window.evWrites = [];
        window.fetch = async (input, options = {}) => {
            if (new URL(input, location.href).pathname === '/v1/ev/requests' && options.method === 'POST') {
                window.evWrites.push({body: JSON.parse(options.body), hasSignal: Boolean(options.signal)});
                const result = await original(input, options);
                if (window.evWrites.length === 1) throw new Error(privateValue);
                return result;
            }
            return original(input, options);
        };
    }""",
        PRIVATE_SENTINEL,
    )
    card.get_by_role("button", name="Plan charging request", exact=True).click()
    expect(card.get_by_role("status")).to_contain_text("could not be confirmed")
    expect(card.get_by_role("status")).not_to_contain_text(PRIVATE_SENTINEL)
    assert page.evaluate("window.evWrites.length") == 1
    assert not page.evaluate("window.evWrites[0].hasSignal")
    page.locator("#refresh-evidence").click()
    assert float(card.get_by_label("Target state of charge (%)", exact=True).input_value()) == 65
    card.get_by_role("button", name="Plan charging request", exact=True).click()
    expect(card).to_contain_text("Planned charging request")
    writes = page.evaluate("window.evWrites")
    assert len(writes) == 2
    assert writes[0]["body"]["request_id"] == writes[1]["body"]["request_id"]
    assert len(page.request.get(url + "/v1/ev/requests", headers=headers).json()["requests"]) == 1
