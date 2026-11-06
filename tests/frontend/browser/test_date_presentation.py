"""Home Assistant date preferences affect labels, never calendar values or instants."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize(
    ("preferences", "expected"),
    [
        ({"language": "nl", "date_format": "language"}, "1-6-2026"),
        ({"language": "en-US", "date_format": "DMY"}, "1/6/2026"),
        ({"language": "nl", "date_format": "MDY"}, "6-1-2026"),
        ({"language": "nl", "date_format": "YMD"}, "2026-6-1"),
        ({"language": "nl", "date_format": "system"}, "6/1/2026"),
        ({"language": "not_a_locale", "date_format": "toString"}, "6/1/2026"),
    ],
)
def test_home_assistant_date_preferences_preserve_calendar_values(app_page, preferences, expected):
    """Read the HA host preference while preserving Amsterdam dates and exact UTC conversion."""
    page, url = app_page
    page.goto(url + "/contract")
    result = page.evaluate(
        """async preferences => {
            const host = document.createElement('home-assistant');
            host.hass = {locale: preferences}; document.body.append(host);
            const display = await import('/setup/display.js');
            display.useHomeAssistantTimezone('Europe/Amsterdam');
            return {
                calendar: display.dateLabel('2026-06-01'),
                instant: display.dateLabel('2026-05-31T22:30:59Z'),
                combined: display.when('2026-05-31T22:30:59Z'),
                stored: display.localDate('2026-05-31T22:30:59Z'),
                exact: display.localInstant('2026-06-01T00:30:59'),
            };
        }""",
        preferences,
    )
    assert result["calendar"] == result["instant"] == expected
    assert result["combined"].startswith(expected)
    assert result["stored"] == "2026-06-01"
    assert result["exact"] == "2026-05-31T22:30:59.000Z"


def test_home_assistant_preferences_reach_nested_content_and_refresh(app_page):
    """The same-origin HA host owns nested date labels, including a preference changed before rendering."""
    page, url = app_page
    page.goto(url + "/contract")
    page.evaluate(
        """url => {
            const host = document.createElement('home-assistant');
            host.hass = {locale: {language:'nl', date_format:'DMY'}};
            document.body.replaceChildren(host);
            const shell = document.createElement('iframe'); shell.src = url + '/contract?date-shell=1';
            shell.id = 'date-shell'; document.body.append(shell);
        }""",
        url,
    )
    frame = page.frame_locator("#date-shell")
    frame.get_by_role("heading", name="Contract settings", exact=True).wait_for()
    frame.locator("body").evaluate(
        """(body, url) => {
            const child = document.createElement('iframe');
            child.id = 'date-content'; child.src = url + '/contract?date-content=1'; body.append(child);
        }""",
        url,
    )
    content = frame.frame_locator("#date-content")
    content.get_by_role("heading", name="Contract settings", exact=True).wait_for(state="attached")
    result = content.locator("body").evaluate(
        """async () => {
            const display = await import('/setup/display.js');
            const before = display.dateLabel('2026-06-01');
            top.document.querySelector('home-assistant').hass.locale.date_format = 'YMD';
            return [before, display.dateLabel('2026-06-01')];
        }"""
    )
    assert result == ["1-6-2026", "2026-6-1"]


def test_cross_origin_host_cannot_change_date_preferences(app_page):
    """An inaccessible parent cannot supply HA preferences. The child keeps its own browser fallback."""
    page, url = app_page
    page.route(
        url + "/date-preview",
        lambda route: route.fulfill(
            content_type="text/html",
            body="<p id='date'>Loading</p><script type='module'>import {dateLabel} from '/setup/display.js'; document.querySelector('#date').textContent=dateLabel('2026-06-01');</script>",
        ),
    )
    page.route(
        "http://localhost:12345/date-host",
        lambda route: route.fulfill(
            content_type="text/html",
            body=f"<home-assistant></home-assistant><script>document.querySelector('home-assistant').hass={{locale:{{language:'nl',date_format:'DMY'}}}}</script><iframe id='dates' src='{url}/date-preview'></iframe>",
        ),
    )
    page.goto("http://localhost:12345/date-host")
    content = page.frame_locator("#dates")
    expect(content.locator("#date")).to_have_text("6/1/2026")


def test_contract_labels_use_home_assistant_date_format_without_saving(app_page):
    """Dutch HA dates replace ISO rule labels and summaries while the saved contract remains byte-for-byte equivalent."""
    page, url = app_page
    discover(page, url)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = page.request.put(
        url + "/v1/contracts",
        headers=headers,
        data={
            "contract_id": "date-example",
            "market_area": "NL",
            "currency": "EUR",
            "expected_version": None,
            "periods": [
                {
                    "contract_start": "2026-06-01",
                    "effective_from": "2026-06-01",
                    "effective_until": None,
                    "import_formula": "market",
                    "export_formula": "market",
                }
            ],
            "bonuses": [],
        },
    )
    assert response.ok, response.text()
    before = page.request.get(url + "/v1/contracts", headers=headers).json()
    page.goto(url + "/contract")
    page.evaluate("""() => {
        const host = document.createElement('home-assistant');
        host.hass = {locale:{language:'nl',date_format:'DMY'}};
        host.hidden = true; document.body.append(host);
    }""")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#price-rule-select option")).to_have_text("Price rule: 1-6-2026: Active")
    expect(page.locator("#rule-context")).to_have_text("In effect: 1-6-2026")
    expect(page.locator("#contract-summary dd").nth(0)).to_have_text("1-6-2026")
    expect(page.locator("#contract-summary dd").nth(1)).to_have_text("1-6-2026")
    assert page.request.get(url + "/v1/contracts", headers=headers).json() == before
    expect(page.locator("#contract-preview tbody tr")).to_have_count(5)
    capture = Path("results/ci/ha-dates")
    capture.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(capture / f"contract-{page.viewport_size['width']}.png"), full_page=True
    )
