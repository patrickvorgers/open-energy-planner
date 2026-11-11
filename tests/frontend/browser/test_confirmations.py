"""Verify shared modal consent, keyboard behavior and literal untrusted text in the real browser."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


@pytest.mark.parametrize("dismissal", ["Cancel", "Escape", "Enter", "backdrop"])
def test_confirmation_requires_explicit_consent_and_restores_focus(app_page, dismissal):
    """Every dismissal keeps state unchanged, traps focus and returns it to the initiating control."""
    page, url = app_page
    page.goto(url + "/appliance-settings/ev")
    page.evaluate("""async () => {
        const {confirmAction} = await import('/setup/confirmation.js');
        const opener = document.createElement('button');
        opener.textContent = 'Synthetic removal'; document.querySelector('main').append(opener);
        window.confirmationWrites = 0;
        opener.onclick = async () => {
            if (await confirmAction({title:'Remove synthetic device?', message:'Source and history are retained.', confirmLabel:'Remove device'})) window.confirmationWrites++;
        };
    }""")
    opener = page.get_by_role("button", name="Synthetic removal", exact=True)
    opener.click()
    dialog = page.get_by_role("alertdialog", name="Remove synthetic device?", exact=True)
    cancel = dialog.get_by_role("button", name="Cancel", exact=True)
    expect(cancel).to_be_focused()
    cancel.press("Shift+Tab")
    expect(dialog.get_by_role("button", name="Remove device", exact=True)).to_be_focused()
    page.keyboard.press("Tab")
    expect(cancel).to_be_focused()
    assert dialog.evaluate("element => element.scrollWidth <= element.clientWidth")
    if dismissal == "Cancel":
        cancel.click()
    elif dismissal == "backdrop":
        page.mouse.click(2, 2)
    else:
        cancel.press(dismissal)
    expect(dialog).to_have_count(0)
    expect(opener).to_be_focused()
    assert page.evaluate("window.confirmationWrites") == 0


def test_confirmation_escapes_names_and_rejects_overlapping_requests(app_page):
    """Concurrent callers cannot inherit consent, and hostile names stay readable literal text."""
    page, url = app_page
    page.goto(url + "/appliance-settings/ev")
    page.evaluate("""async () => {
        const {confirmAction} = await import('/setup/confirmation.js');
        window.syntheticConfirmationResults = [];
        const options = {title:'Remove device?', message:'Car <img src=x onerror="window.syntheticLeak=true"> & history', confirmLabel:'Remove device'};
        confirmAction(options).then(value => window.syntheticConfirmationResults.push(['first', value]));
        confirmAction(options).then(value => window.syntheticConfirmationResults.push(['second', value]));
    }""")
    dialog = page.get_by_role("alertdialog", name="Remove device?", exact=True)
    expect(dialog).to_have_count(1)
    expect(dialog).to_contain_text('Car <img src=x onerror="window.syntheticLeak=true"> & history')
    expect(dialog.locator("img")).to_have_count(0)
    assert page.evaluate("window.syntheticLeak || false") is False
    dialog.get_by_role("button", name="Remove device", exact=True).dblclick()
    page.wait_for_function("() => window.syntheticConfirmationResults.length === 2")
    assert sorted(page.evaluate("window.syntheticConfirmationResults")) == [
        ["first", True],
        ["second", False],
    ]


@pytest.fixture
def scheduled_appliances(app_page):
    """Provide two independent scheduled tasks and record real cancellation requests."""
    page, url = app_page
    state = {
        "revision": "retained-fixture",
        "issue": None,
        "devices": [
            {
                "entity_id": entity,
                "name": name,
                "shiftable": True,
                "profile": None,
                "task": {
                    "id": task_id,
                    "status": "scheduled",
                    "deadline": "2026-11-07T18:00:00Z",
                    "scheduled_start": None,
                    "attempt": None,
                    "events": [],
                },
            }
            for entity, name, task_id in (
                ("sensor.dishwasher_energy", "Dishwasher", "dishwasher-original"),
                ("sensor.dryer_energy", "Dryer", "dryer-original"),
            )
        ],
    }
    writes = []

    def cancel_task(route):
        """Record the real POST and model its accepted cancellation without physical device access."""
        writes.append(route.request.post_data_json)
        state["devices"][0]["task"]["status"] = "cancelled"
        route.fulfill(json={"appliances": state})

    page.route("**/v1/appliances", lambda route: route.fulfill(json=state))
    page.route("**/v1/appliances/cancel", cancel_task)
    page.route(
        "**/setup/session",
        lambda route: route.fulfill(json={"ingress": False, "timezone": "Europe/Amsterdam"}),
    )
    page.route(
        "**/v1/consumers{,?view=settings}",
        lambda route: route.fulfill(
            json={
                "thermal_meters": [],
                "charging_meters": [],
                "selected": [{"entity_id": item["entity_id"]} for item in state["devices"]],
                "options": [
                    {"entity_id": item["entity_id"], "name": item["name"]}
                    for item in state["devices"]
                ],
            }
        ),
    )
    page.goto(url + "/appliances")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    card = page.locator('[data-appliance-entity="sensor.dishwasher_energy"]')
    expect(card.get_by_role("button", name="Cancel planning", exact=True)).to_be_visible()
    return page, state, writes


@pytest.mark.parametrize("dismissal", ["Confirm", "Cancel", "Escape"])
def test_refreshed_appliance_card_declines_stale_cancel_and_explains_retry(
    scheduled_appliances, dismissal
):
    """A real Refresh rejects stale consent and restores visible focus for every dismissal."""
    page, state, writes = scheduled_appliances
    held = []

    def hold_refresh(route):
        """Keep the old card actionable until its actual replacement response arrives."""
        held.append(route)
        page.evaluate("window.confirmationRefreshHeld = true")

    page.route("**/v1/appliances", hold_refresh, times=1)
    card = page.locator('[data-appliance-entity="sensor.dishwasher_energy"]')
    original_cancel = card.get_by_role(
        "button", name="Cancel planning", exact=True
    ).element_handle()
    page.get_by_role("button", name="Refresh task evidence", exact=True).click()
    page.wait_for_function("() => window.confirmationRefreshHeld === true")
    card.get_by_role("button", name="Cancel planning", exact=True).click()
    confirmation = page.get_by_role("alertdialog", name="Cancel appliance planning?", exact=True)
    expect(confirmation).to_contain_text("Dishwasher")
    state["devices"][0]["task"]["id"] = "dishwasher-refreshed"
    held[0].fulfill(json=state)
    page.wait_for_function("button => !button.isConnected", arg=original_cancel)
    if dismissal == "Escape":
        page.keyboard.press("Escape")
    else:
        confirmation.get_by_role(
            "button", name="Cancel planning" if dismissal == "Confirm" else "Cancel", exact=True
        ).click()
    expect(confirmation).to_have_count(0)
    expect(page.get_by_role("heading", name="Appliances", exact=True)).to_be_focused()
    assert writes == []
    expect(card).to_contain_text("Task: scheduled")
    if dismissal != "Confirm":
        return
    expect(page.locator("#status")).to_have_text(
        "Dishwasher: Information was refreshed while confirmation was open. Review the current task, then choose Cancel planning again if needed."
    )
    card.get_by_role("button", name="Cancel planning", exact=True).click()
    confirmation.get_by_role("button", name="Cancel planning", exact=True).dblclick()
    expect(card).to_contain_text("Task: cancelled")
    assert writes == [{"entity_id": "sensor.dishwasher_energy", "task_id": "dishwasher-refreshed"}]
    expect(page.locator('[data-appliance-entity="sensor.dryer_energy"]')).to_contain_text(
        "Task: scheduled"
    )


def test_failed_cancellation_hides_private_response_and_explains_recovery(scheduled_appliances):
    """An ambiguous server failure gives owned recovery advice without leaking response details."""
    page, state, writes = scheduled_appliances
    private_response = "private-server-sentinel: database credential synthetic-secret"
    page.route(
        "**/v1/appliances/cancel",
        lambda route: route.fulfill(status=503, body=private_response),
    )
    card = page.locator('[data-appliance-entity="sensor.dishwasher_energy"]')
    card.get_by_role("button", name="Cancel planning", exact=True).click()
    page.get_by_role("alertdialog").get_by_role("button", name="Cancel planning").click()
    expect(page.locator("#status")).to_have_text(
        "Dishwasher: Cancellation could not be confirmed. Choose Refresh task evidence to check the current task before trying again. Physical control remains with Home Assistant."
    )
    expect(page.locator("body")).not_to_contain_text(private_response)
    expect(card.get_by_role("button", name="Cancel planning", exact=True)).to_be_enabled()
    assert writes == []
    assert [device["task"]["status"] for device in state["devices"]] == ["scheduled", "scheduled"]


def test_confirmation_preserves_focus_deliberately_moved_after_close(app_page):
    """Closing a dialog must not steal focus from an explicitly chosen visible destination."""
    page, url = app_page
    page.goto(url + "/appliances")
    page.get_by_label("Installation API token", exact=True).focus()
    page.evaluate("""async () => {
        const {confirmAction} = await import('/setup/confirmation.js');
        window.focusConfirmation = confirmAction({title:'Cancel task?', message:'Synthetic task.', confirmLabel:'Cancel task'});
    }""")
    expect(page.get_by_role("alertdialog")).to_be_visible()
    page.evaluate("""() => {
        document.querySelector('dialog').close('cancel');
        document.querySelector('#connect button').focus();
    }""")
    page.wait_for_function("async () => await window.focusConfirmation === false")
    expect(page.get_by_role("button", name="Connect", exact=True)).to_be_focused()
