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


def test_refreshed_appliance_card_declines_stale_cancel_and_explains_retry(app_page):
    """A real held Refresh replaces the card, rejects old consent and permits only a fresh task-specific retry."""
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
    held, writes = [], []
    hold_next = False

    def appliance_read(route):
        """Hold one actual Refresh response while leaving the original card actionable."""
        nonlocal hold_next
        if hold_next:
            hold_next = False
            held.append(route)
            page.evaluate("window.confirmationRefreshHeld = true")
        else:
            route.fulfill(json=state)

    def cancel_task(route):
        """Record the real POST and model its accepted cancellation without physical device access."""
        writes.append(route.request.post_data_json)
        state["devices"][0]["task"]["status"] = "cancelled"
        route.fulfill(json={"appliances": state})

    page.route("**/v1/appliances", appliance_read)
    page.route("**/v1/appliances/cancel", cancel_task)
    page.route(
        "**/setup/session",
        lambda route: route.fulfill(json={"ingress": False, "timezone": "Europe/Amsterdam"}),
    )
    page.route(
        "**/v1/consumers",
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
    original_cancel = card.get_by_role(
        "button", name="Cancel planning", exact=True
    ).element_handle()
    hold_next = True
    page.get_by_role("button", name="Refresh task evidence", exact=True).click()
    page.wait_for_function("() => window.confirmationRefreshHeld === true")
    card.get_by_role("button", name="Cancel planning", exact=True).click()
    confirmation = page.get_by_role("alertdialog", name="Cancel appliance planning?", exact=True)
    expect(confirmation).to_contain_text("Dishwasher")
    state["devices"][0]["task"]["id"] = "dishwasher-refreshed"
    held[0].fulfill(json=state)
    page.wait_for_function("button => !button.isConnected", arg=original_cancel)
    confirmation.get_by_role("button", name="Cancel planning", exact=True).click()
    expect(confirmation).to_have_count(0)
    assert writes == []
    expect(card).to_contain_text("Task: scheduled")
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
