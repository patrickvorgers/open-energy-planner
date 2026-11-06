"""Verify shared modal consent, keyboard behavior and literal untrusted text in the real browser."""

import pytest
from playwright.sync_api import expect


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
