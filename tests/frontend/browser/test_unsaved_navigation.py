"""Protect configuration drafts with app dialogs during internal navigation."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover, edit_price_rule, open_primary_navigation


def add_vehicle_draft(content):
    """Use the actual typed Add flow and change an unsaved vehicle's capacity."""
    content.get_by_role("button", name="Add vehicle or charge point", exact=True).click()
    content.get_by_role("dialog", name="Add vehicle or charge point", exact=True).get_by_role(
        "button", name="Continue", exact=True
    ).click()
    capacity = content.get_by_label("Usable battery capacity (kWh)", exact=True)
    capacity.fill("74")
    return capacity


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize("workspace", [False, True, "home_assistant"])
@pytest.mark.parametrize("destination_kind", ["menu", "category"])
def test_ev_draft_navigation_uses_custom_confirmation_and_preserves_cancelled_edits(
    app_page, workspace, destination_kind
):
    """A new unsaved vehicle survives Cancel and leaves once after explicit discard consent."""
    page, url = app_page
    discover(page, url)
    destination_url = url + (
        "/workspace?page=%2Fappliance-settings%2Fev" if workspace else "/appliance-settings/ev"
    )
    if workspace == "home_assistant":
        page.route(
            url + "/ha-host",
            lambda route: route.fulfill(
                content_type="text/html",
                body=f'<body style="margin:0"><iframe title="Home Assistant app" src="{destination_url}" style="width:100vw;height:100vh;border:0"></iframe></body>',
            ),
        )
        page.goto(url + "/ha-host")
        shell = page.frames[1]
    else:
        page.goto(destination_url)
        shell = page
    content = shell.frame_locator("#workspace-content") if workspace else page
    expect(content.locator("#settings")).to_be_visible()
    capacity = add_vehicle_draft(content)
    native_dialogs = []

    def dismiss_native(dialog):
        """Record unexpected native prompts without allowing them to discard the draft."""
        native_dialogs.append(dialog.type)
        dialog.dismiss()

    page.on("dialog", dismiss_native)
    if destination_kind == "menu":
        open_primary_navigation(shell)
    destination = (
        shell.get_by_role("region", name="Settings", exact=True).get_by_role(
            "link", name="Contract", exact=True
        )
        if destination_kind == "menu"
        else content.get_by_role("navigation", name="Appliance settings", exact=True).get_by_role(
            "link", name="Electric boilers", exact=True
        )
    )
    destination.click()
    confirmation = content.get_by_role("alertdialog", name="Leave without saving?", exact=True)
    expect(confirmation).to_be_visible()
    expect(confirmation.get_by_role("button", name="Cancel", exact=True)).to_be_focused()
    if destination_kind == "menu":
        Path("results/ci").mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f"results/ci/unsaved-ev-{page.viewport_size['width']}-{workspace}.png")
    confirmation.get_by_role("button", name="Cancel", exact=True).click()
    expect(capacity).to_have_value("74")
    expect(content.locator("#dirty")).to_contain_text("Unsaved changes")
    assert (
        page.request.get(url + "/v1/ev", headers={"Authorization": "Bearer " + TOKEN}).json()[
            "vehicles"
        ]
        == []
    )
    if destination_kind == "menu":
        open_primary_navigation(shell)
    destination.click()
    confirmation.get_by_role("button", name="Discard and leave", exact=True).click()
    if destination_kind == "menu":
        expect(content.get_by_role("heading", name="Contract settings", exact=True)).to_be_visible()
    else:
        expect(content.locator("#thermal-settings")).to_be_visible()
    assert native_dialogs == []


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize("kind", ["contract", "boiler", "consumer"])
def test_other_configuration_editors_share_navigation_consent(app_page, kind):
    """Contract, boiler and consumer drafts use the same dialog without persisting a write."""
    page, url = app_page
    body = discover(page, url)
    body["entities"].append(
        {
            "entity_id": "sensor.synthetic_washer",
            "name": "Synthetic washer",
            "unit": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
            "device_id": "washer",
        }
    )
    body["suggestions"]["consumers"] = [{"entity_id": "sensor.synthetic_washer", "upstream": None}]
    headers = {"Authorization": "Bearer " + TOKEN}
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=body).ok
    route = {
        "contract": "/contract",
        "boiler": "/appliance-settings/thermal",
        "consumer": "/appliance-settings",
    }[kind]
    page.goto(url + route)
    if kind == "contract":
        edit_price_rule(page)
        page.get_by_label("Import formula", exact=True).fill("market + 0.187")
    elif kind == "boiler":
        page.get_by_role("button", name="Add electric boiler", exact=True).click()
    else:
        page.get_by_role("button", name="Add appliance", exact=True).click()
        page.locator(".source-choice").filter(has_text="Synthetic washer").click()
    open_primary_navigation(page)
    page.get_by_role("link", name="Data storage", exact=True).click()
    confirmation = page.get_by_role("alertdialog", name="Leave without saving?", exact=True)
    expect(confirmation).to_be_visible()
    confirmation.press("Escape")
    expect(page).to_have_url(url + route)
    if kind == "contract":
        expect(page.get_by_label("Import formula", exact=True)).to_have_value("market + 0.187")
    elif kind == "boiler":
        expect(page.locator("#thermal-save-state")).to_contain_text("Unsaved changes")
    else:
        expect(page.locator("#save-state")).to_contain_text("Unsaved changes")
        assert page.request.get(url + "/v1/consumers", headers=headers).json()["selected"] == []


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize("workspace", [False, True])
def test_real_browser_reload_retains_native_unsaved_draft_protection(app_page, workspace):
    """Cancel in the custom dialog does not disable the browser's own later reload warning."""
    page, url = app_page
    discover(page, url)
    page.goto(
        url
        + ("/workspace?page=%2Fappliance-settings%2Fev" if workspace else "/appliance-settings/ev")
    )
    content = page.frame_locator("#workspace-content") if workspace else page
    expect(content.locator("#settings")).to_be_visible()
    capacity = add_vehicle_draft(content)
    content.get_by_role("navigation", name="Appliance settings", exact=True).get_by_role(
        "link", name="Electric boilers", exact=True
    ).click()
    content.get_by_role("alertdialog").press("Escape")
    with page.expect_event("dialog") as native:
        page.evaluate("setTimeout(() => location.reload(), 0)")
    assert native.value.type == "beforeunload"
    native.value.dismiss()
    expect(capacity).to_have_value("74")
    expect(content.get_by_role("alertdialog")).to_have_count(0)


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_navigation_rechecks_draft_after_consent_and_restores_unload_guard(app_page):
    """Asynchronous draft changes reject stale consent and still protect a subsequent browser reload."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/appliance-settings/ev")
    expect(page.locator("#settings")).to_be_visible()
    capacity = add_vehicle_draft(page)
    page.get_by_role("navigation", name="Appliance settings", exact=True).get_by_role(
        "link", name="Electric boilers", exact=True
    ).click()
    capacity.evaluate(
        "input => { input.value = '75'; input.dispatchEvent(new Event('input', {bubbles: true})); }"
    )
    page.get_by_role("alertdialog").get_by_role(
        "button", name="Discard and leave", exact=True
    ).click()
    expect(page).to_have_url(url + "/appliance-settings/ev")
    expect(capacity).to_have_value("75")
    expect(page.locator("#dirty")).to_contain_text("Settings changed while confirmation was open")
    with page.expect_event("dialog") as native:
        page.evaluate("setTimeout(() => location.reload(), 0)")
    assert native.value.type == "beforeunload"
    native.value.dismiss()


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_navigation_waits_for_save_and_preserves_failed_write_edits(app_page):
    """A held real save blocks navigation, and a failed save leaves the edited vehicle available."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/appliance-settings/ev")
    expect(page.locator("#settings")).to_be_visible()
    capacity = add_vehicle_draft(page)
    page.get_by_label("Home Assistant integration", exact=True).select_option("other")
    page.get_by_label("Vehicle state of charge", exact=True).select_option("sensor.soc0")
    page.get_by_label("Maximum vehicle AC charging power (kW)", exact=True).fill("11")
    held = []
    page.route("**/v1/ev/configuration", lambda route: held.append(route))
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(capacity).to_be_disabled()
    open_primary_navigation(page)
    page.get_by_role("link", name="Contract", exact=True).click()
    expect(page.locator("#dirty")).to_contain_text("Wait for the current save or refresh to finish")
    expect(page.get_by_role("alertdialog")).to_have_count(0)
    expect(page).to_have_url(url + "/appliance-settings/ev")
    assert len(held) == 1
    held[0].fulfill(status=503, body="Synthetic unavailable save")
    expect(capacity).to_be_enabled()
    expect(capacity).to_have_value("74")
    open_primary_navigation(page)
    page.get_by_role("link", name="Contract", exact=True).click()
    page.get_by_role("alertdialog").get_by_role("button", name="Cancel", exact=True).click()
    expect(capacity).to_have_value("74")


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_same_page_reload_is_guarded_but_fragments_and_new_tabs_keep_the_draft(app_page):
    """A same-URL document reload needs consent, while fragment and new-tab links do not discard edits."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/appliance-settings/ev")
    expect(page.locator("#settings")).to_be_visible()
    capacity = add_vehicle_draft(page)
    page.get_by_role("navigation", name="Appliance settings", exact=True).get_by_role(
        "link", name="EV charging", exact=True
    ).click()
    page.get_by_role("alertdialog").press("Escape")
    page.evaluate("""() => {
        const fragment = document.createElement('a');
        fragment.href = '#ev-editor'; fragment.textContent = 'Jump to vehicle editor';
        document.querySelector('main').append(fragment);
    }""")
    page.get_by_role("link", name="Jump to vehicle editor", exact=True).click()
    expect(page.get_by_role("alertdialog")).to_have_count(0)
    expect(capacity).to_have_value("74")
    page.evaluate("""() => {
        const download = document.createElement('a');
        download.href = new URL('../../setup/icons/app.svg', location.href);
        download.download = 'synthetic-app.svg'; download.textContent = 'Download app icon';
        document.querySelector('main').append(download);
    }""")
    with page.expect_download():
        page.get_by_role("link", name="Download app icon", exact=True).click()
    expect(page.get_by_role("alertdialog")).to_have_count(0)
    expect(capacity).to_have_value("74")
    page.get_by_role("navigation", name="Appliance settings", exact=True).get_by_role(
        "link", name="Electric boilers", exact=True
    ).evaluate("link => link.target = '_blank'")
    with page.expect_popup() as opened:
        page.get_by_role("navigation", name="Appliance settings", exact=True).get_by_role(
            "link", name="Electric boilers", exact=True
        ).click()
    opened.value.close()
    expect(page.get_by_role("alertdialog")).to_have_count(0)
    expect(capacity).to_have_value("74")


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_a_withdrawn_link_does_not_spend_consent_on_a_later_browser_unload(app_page):
    """Removing an asynchronously replaced link cancels its navigation without disabling future unload protection."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/appliance-settings/ev")
    expect(page.locator("#settings")).to_be_visible()
    capacity = add_vehicle_draft(page)
    destination = page.get_by_role("navigation", name="Appliance settings", exact=True).get_by_role(
        "link", name="Electric boilers", exact=True
    )
    destination.click()
    destination.evaluate("link => link.remove()")
    page.get_by_role("alertdialog").get_by_role(
        "button", name="Discard and leave", exact=True
    ).click()
    expect(capacity).to_have_value("74")
    expect(page.locator("#dirty")).to_contain_text("Unsaved changes")
    with page.expect_event("dialog") as native:
        page.evaluate("setTimeout(() => location.reload(), 0)")
    assert native.value.type == "beforeunload"
    native.value.dismiss()


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_workspace_concurrent_destinations_do_not_replace_pending_consent(app_page):
    """A second shell destination cannot create another dialog or change the first approved destination."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/workspace?page=%2Fappliance-settings%2Fev")
    content = page.frame_locator("#workspace-content")
    expect(content.locator("#settings")).to_be_visible()
    add_vehicle_draft(content)
    open_primary_navigation(page)
    page.get_by_role("link", name="Contract", exact=True).click()
    confirmation = content.get_by_role("alertdialog", name="Leave without saving?", exact=True)
    expect(confirmation).to_be_visible()
    open_primary_navigation(page)
    page.get_by_role("link", name="Data storage", exact=True).click()
    expect(confirmation).to_have_count(1)
    confirmation.get_by_role("button", name="Discard and leave", exact=True).click()
    expect(content.get_by_role("heading", name="Contract settings", exact=True)).to_be_visible()
    expect(page.locator('.sidebar [aria-current="page"]')).to_have_text("Contract")


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_workspace_links_opening_another_tab_do_not_request_discard(app_page):
    """A shell link explicitly targeting a new tab leaves the current vehicle draft untouched."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/workspace?page=%2Fappliance-settings%2Fev")
    content = page.frame_locator("#workspace-content")
    expect(content.locator("#settings")).to_be_visible()
    capacity = add_vehicle_draft(content)
    open_primary_navigation(page)
    destination = page.get_by_role("link", name="Contract", exact=True)
    destination.evaluate("link => link.target = '_blank'")
    with page.expect_popup() as opened:
        destination.click()
    opened.value.close()
    expect(content.get_by_role("alertdialog")).to_have_count(0)
    expect(capacity).to_have_value("74")
    expect(page.locator('.sidebar [aria-current="page"]')).to_have_text("Appliances")
