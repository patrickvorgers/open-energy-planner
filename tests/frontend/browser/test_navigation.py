"""Responsive app-owned setup against real authenticated API/storage, without physical devices."""

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    TOKEN,
    connect,
    discover,
    open_primary_navigation,
    read_settings,
)


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_storage_navigation_authentication_refresh_and_warning(app_page, monkeypatch):
    """Desktop/mobile and ingress show actual storage, low-space alerts, unknowns and recoverable failures."""
    from open_energy_planner.storage.adapters.usage import StorageUsage

    page, url = app_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    open_primary_navigation(page)
    page.get_by_role("link", name="Data storage", exact=True).click()
    expect(page).to_have_url(url + "/data-storage")
    page.wait_for_load_state("domcontentloaded")
    if "/api/hassio_ingress/" not in url:
        page.get_by_label("Installation API token").fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#storage")).to_be_visible()
    expect(page.locator("#backend")).to_have_text("SQLite")
    expect(page.locator("#database-size")).not_to_have_text("Unknown")
    assert (
        page.locator(".sidebar a[aria-current='page']").get_attribute("href")
        == urlsplit(url).path + "/data-storage"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    usage = StorageUsage(
        "sqlite", database_bytes=2048, disk_total_bytes=100 * 1024**3, disk_free_bytes=0
    )
    monkeypatch.setattr(
        "open_energy_planner.storage.adapters.usage.storage_usage", lambda _engine: usage
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#storage-warning")).to_be_visible()
    expect(page.locator("#free-space")).to_have_text("0 B")
    expect(page.locator("#capacity-status")).to_have_text("Low space")
    page.screenshot(
        path=f"results/ci/storage-low-{page.viewport_size['width']}-{'ingress' in url}.png",
        full_page=True,
    )
    usage.disk_free_bytes = 10 * 1024**3
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#storage-warning")).to_be_hidden()
    expect(page.locator("#capacity-status")).to_have_text("Sufficient space")
    usage.backend = "mysql"
    usage.database_estimated = True
    usage.disk_free_bytes = usage.disk_total_bytes = None
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#free-space")).to_have_text("Unknown")
    expect(page.locator("#database-size")).to_have_text("2 KiB (estimated)")
    expect(page.locator("#capacity-status")).to_have_text("Unknown")
    page.route("**/v1/storage", lambda route: route.fulfill(status=503, body="private/path secret"))
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Storage evidence unavailable")
    expect(page.locator("#storage")).to_be_hidden()
    assert "secret" not in page.locator("body").inner_text()
    page.unroute("**/v1/storage")
    usage.measurement_failed = True
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#capacity-status")).to_have_text("Measurement unavailable")
    expect(page.locator("#status")).to_contain_text("Some storage measurements are unavailable")
    expect(page.get_by_role("button", name="Refresh", exact=True)).to_be_enabled()
    assert not errors


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize(
    "route",
    [
        "/overview",
        "/appliances",
        "/ev",
        "/thermal",
        "/quality",
        "/planning-performance",
        "/setup",
        "/contract",
        "/data-storage",
        "/appliance-settings",
        "/appliance-settings/thermal",
        "/appliance-settings/ev",
    ],
)
def test_shared_sidebar_refreshes_integration_status_on_every_page(app_page, route):
    """Every page keeps matching identities quiet and shows a real mismatch through its shared Menu."""
    from open_energy_planner.build_info import BUILD_INFO

    page, url = app_page
    width = page.viewport_size["width"]
    response = page.request.get(
        url + "/v1/version",
        headers={
            "Authorization": "Bearer " + TOKEN,
            "X-OEP-Release": BUILD_INFO["version"],
            "X-OEP-Build": BUILD_INFO["build"] or "",
        },
    )
    assert response.ok
    assert response.json()["status"] == "matched"
    page.goto(url + route)
    menu = page.get_by_role("button", name="Menu", exact=True, include_hidden=True)
    expect(menu).to_have_count(1)
    if width == 390:
        expect(page.locator(".sidebar")).to_be_hidden()
        expect(menu).to_be_visible()
        menu.click()
    else:
        expect(menu).to_be_hidden()
    footer = page.locator(".release-info")
    footer.scroll_into_view_if_needed()
    expect(footer).to_be_in_viewport()
    expect(page.locator("#integration-version-status")).to_be_hidden()
    assert "Integration:" not in footer.inner_text()
    response = page.request.get(
        url + "/v1/version",
        headers={
            "Authorization": "Bearer " + TOKEN,
            "X-OEP-Release": BUILD_INFO["version"],
            "X-OEP-Build": "previous-build",
        },
    )
    assert response.status == 409
    page.reload()
    if width == 390:
        menu.click()
    expect(page.locator("#integration-version-status")).to_be_visible()
    expect(page.locator("#integration-version-status")).to_have_text(
        "Integration: Version mismatch"
    )


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_sidebar_shows_loaded_version_build_and_integration_mismatch(app_page, monkeypatch):
    """Desktop and mobile show version/build alone until a mismatch, then hide its error after recovery or a failed read."""
    from open_energy_planner.build_info import BUILD_INFO, HEADER_FIELDS

    page, url = app_page
    width = page.viewport_size["width"]
    expect(page.locator(".release-info")).to_contain_text("Version: Development")
    monkeypatch.setitem(BUILD_INFO, "build", "142.2")
    monkeypatch.setitem(BUILD_INFO, "revision", "a" * 40)
    monkeypatch.setitem(BUILD_INFO, "release_tag", "v0.2.0")

    def show_navigation():
        """Open the mobile navigation after each reload without editing installation settings."""
        page.goto(url + "/setup")
        if width == 390:
            page.get_by_role("button", name="Menu", exact=True).click()

    show_navigation()
    footer = page.locator(".release-info")
    expect(footer).to_be_visible()
    expect(footer).to_contain_text("Version: " + BUILD_INFO["version"])
    expect(footer).to_contain_text("Build: 142.2")
    expect(footer).not_to_contain_text("Commit")
    expect(footer).not_to_contain_text("aaaaaaa")
    expect(page.locator("#integration-version-status")).to_be_hidden()
    assert "Integration:" not in footer.inner_text()
    headers = {
        "Authorization": "Bearer " + TOKEN,
        **{header: BUILD_INFO[field] or "" for field, header in HEADER_FIELDS.items()},
    }
    assert page.request.get(url + "/v1/version", headers=headers).ok
    show_navigation()
    expect(page.locator("#integration-version-status")).to_be_hidden()
    assert "Integration:" not in footer.inner_text()
    footer.scroll_into_view_if_needed()
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    page.screenshot(path=f"results/ci/software-version-{width}.png")
    response = page.request.get(url + "/v1/version", headers={**headers, "X-OEP-Build": "141.1"})
    assert response.status == 409
    show_navigation()
    expect(footer).to_contain_text("Integration: Version mismatch")
    expect(page.locator("#integration-version-status")).to_be_visible()
    expect(page.locator("#integration-version-detail")).to_be_visible()
    contrast = page.locator("#integration-version-status").evaluate(
        """element => {
            const luminance = color => {
                const channels = color.match(/[0-9.]+/g).slice(0, 3).map(value => {
                    const channel = Number(value) / 255;
                    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
                });
                return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
            };
            const foreground = luminance(getComputedStyle(element).color);
            const background = luminance(getComputedStyle(element.closest('.sidebar')).backgroundColor);
            return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
        }"""
    )
    assert contrast >= 4.5
    expect(page.locator("#integration-version-detail")).to_contain_text("Build: 141.1")
    assert page.request.get(url + "/v1/version", headers=headers).ok
    show_navigation()
    expect(page.locator("#integration-version-status")).to_be_hidden()
    expect(page.locator("#integration-version-detail")).to_be_hidden()
    assert (
        page.request.get(
            url + "/v1/version",
            headers={"Authorization": "Bearer " + TOKEN, "X-OEP-View": "commands"},
        ).status
        == 409
    )
    show_navigation()
    expect(page.locator("#integration-version-detail")).to_contain_text(
        "Integration version: Unknown"
    )
    expect(page.locator("#integration-version-status")).to_have_attribute(
        "title", "Last observed integration: Unknown: Build: Development"
    )
    page.route("**/setup/session", lambda route: route.fulfill(status=503, body="Unavailable"))
    show_navigation()
    expect(page.locator("#integration-version-status")).to_be_hidden()
    expect(page.locator("#integration-version-detail")).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_menu_navigation_can_be_cancelled_without_losing_installation_edits(app_page):
    """Native document navigation still honors the existing unsaved-installation warning."""
    page, url = app_page
    discover(page, url)
    if "/api/hassio_ingress/" in url:
        page.reload()
        expect(page.locator("#installation-shell")).to_be_visible()
        page.get_by_role("button", name="Continue", exact=True).click()
    else:
        connect(page)
    grid_import_limit_input = page.get_by_label("Maximum grid import (kW)", exact=True)
    grid_import_limit_input.fill("6.3")
    open_primary_navigation(page)
    dialogs = []

    def keep_draft(dialog):
        """Cancel a genuine beforeunload prompt rather than navigating away with unsaved edits."""
        dialogs.append(dialog.type)
        dialog.dismiss()

    page.once("dialog", keep_draft)
    page.get_by_role("region", name="Settings", exact=True).get_by_role(
        "link", name="Contract", exact=True
    ).click(no_wait_after=True)
    expect(page).to_have_url(url + "/setup")
    expect(grid_import_limit_input).to_have_value("6.3")
    assert dialogs == ["beforeunload"]


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
@pytest.mark.parametrize("inside_home_assistant", [False, True])
def test_workspace_keeps_sidebar_and_document_lifecycles_separate(app_page, inside_home_assistant):
    """Only the content document navigates, including within HA's iframe and browser history."""
    page, url = app_page
    if page.viewport_size["width"] > 640:
        page.set_viewport_size({"width": 1920, "height": 1080})
    destination = url + "/workspace?page=%2Fdata-storage"
    if inside_home_assistant:
        page.route(
            url + "/ha-host",
            lambda route: route.fulfill(
                content_type="text/html",
                body=f'<body style="margin:0"><iframe title="Home Assistant app" src="{destination}" onload="this.style.display=\'block\'" style="display:none;width:100vw;height:100vh;border:0"></iframe></body>',
            ),
        )
        page.goto(url + "/ha-host")
        shell = page.frames[1]
    else:
        page.goto(destination)
        shell = page
    content = shell.frame_locator("#workspace-content")
    expect(content.get_by_role("heading", name="Data storage", exact=True)).to_be_visible()
    assert shell.locator("#workspace-content").evaluate(
        "element => Math.abs(element.getBoundingClientRect().right - innerWidth) <= 1"
    )
    expect(content.locator(".sidebar")).to_be_hidden()
    if page.viewport_size["width"] < 640:
        open_primary_navigation(shell)
        content.locator("body").click(position={"x": page.viewport_size["width"] - 5, "y": 10})
        expect(shell.get_by_role("navigation", name="Primary navigation")).to_be_hidden()
        open_primary_navigation(shell)
        content.locator("body").press("Escape")
        expect(shell.get_by_role("navigation", name="Primary navigation")).to_be_hidden()
        expect(shell.get_by_role("button", name="Menu", exact=True)).to_be_focused()
    shell.evaluate(
        "window.originalSidebar = document.querySelector('.sidebar'); window.shellMarker = 'retained'"
    )
    shell.locator("#workspace-content").evaluate(
        "element => element.contentWindow.oldPageMarker = 'old storage document'"
    )
    open_primary_navigation(shell)
    shell.get_by_role("region", name="Settings", exact=True).get_by_role(
        "link", name="Contract", exact=True
    ).press("Enter")
    expect(content.get_by_role("heading", name="Contract settings", exact=True)).to_be_visible()
    expect(shell.locator('.sidebar [aria-current="page"]')).to_have_text("Contract")
    assert shell.evaluate(
        "window.shellMarker === 'retained' && originalSidebar === document.querySelector('.sidebar')"
    )
    assert shell.locator("#workspace-content").evaluate(
        "element => element.contentWindow.oldPageMarker === undefined"
    )
    assert shell.evaluate("new URL(location.href).searchParams.get('page')") == "/contract"
    assert (
        shell.locator("#workspace-content").evaluate(
            "element => element.contentDocument.activeElement.tagName"
        )
        == "H1"
    )
    assert shell.locator("#workspace-content").evaluate(
        "element => element.contentDocument.documentElement.scrollWidth <= element.contentWindow.innerWidth"
    )
    page.screenshot(
        path=f"results/ci/workspace-{page.viewport_size['width']}-{inside_home_assistant}-{'ingress' in url}.png",
        full_page=True,
    )
    page.evaluate("history.back()")
    expect(content.get_by_role("heading", name="Data storage", exact=True)).to_be_visible()
    expect(shell.locator('.sidebar [aria-current="page"]')).to_have_text("Data storage")
    assert shell.evaluate("new URL(location.href).searchParams.get('page')") == "/data-storage"
    page.evaluate("history.forward()")
    expect(content.get_by_role("heading", name="Contract settings", exact=True)).to_be_visible()
    expect(shell.locator('.sidebar [aria-current="page"]')).to_have_text("Contract")
    assert shell.evaluate("new URL(location.href).searchParams.get('page')") == "/contract"
    page.reload()
    if inside_home_assistant:
        # Reload the outer HA fixture with the last shared workspace address.
        shell = page.frames[1]
        content = shell.frame_locator("#workspace-content")
    expect(
        content.get_by_role(
            "heading",
            name="Contract settings" if not inside_home_assistant else "Data storage",
            exact=True,
        )
    ).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize(
    "start_page,group,destination",
    [("/data-storage", "Settings", "Data storage"), ("/overview#planning", "Energy", "Savings")],
)
def test_workspace_last_selection_cancels_a_pending_document(
    app_page, start_page, group, destination
):
    """Returning to a committed page or its other view cancels a slow destination without reloading current state."""
    from urllib.parse import quote

    page, url = app_page
    page.goto(url + "/workspace?page=" + quote(start_page, safe=""))
    content = page.frame_locator("#workspace-content")
    expect(page.locator('.sidebar [aria-current="page"]')).to_have_text(
        "Data storage" if group == "Settings" else "Planning"
    )
    page.locator("#workspace-content").evaluate(
        "element => element.contentWindow.retainedMarker = true"
    )
    pending_destination = url + "/contract?embedded=1"
    page.route(pending_destination, lambda route: None)
    open_primary_navigation(page)
    with page.expect_request(pending_destination):
        page.get_by_role("region", name="Settings", exact=True).get_by_role(
            "link", name="Contract", exact=True
        ).click()
    open_primary_navigation(page)
    with page.expect_event(
        "requestfailed",
        predicate=lambda browser_request: browser_request.url == pending_destination,
    ):
        page.get_by_role("region", name=group, exact=True).get_by_role(
            "link", name=destination, exact=True
        ).click()
    expect(content.locator("h1")).to_have_text(destination)
    expect(page.locator('.sidebar [aria-current="page"]')).to_have_text(destination)
    assert (
        page.locator("#workspace-content").evaluate(
            "element => element.contentWindow.retainedMarker"
        )
        is True
    )


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_workspace_can_retry_after_content_connection_failure(app_page):
    """A browser error document cannot disable the parent menu or prevent retrying the failed destination."""
    page, url = app_page
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.goto(url + "/workspace?page=%2Fdata-storage")
    content = page.frame_locator("#workspace-content")
    expect(content.get_by_role("heading", name="Data storage", exact=True)).to_be_visible()
    destination = url + "/contract?embedded=1"
    page.route(destination, lambda route: route.abort("connectionrefused"))
    open_primary_navigation(page)
    with page.expect_event(
        "requestfailed", predicate=lambda browser_request: browser_request.url == destination
    ):
        page.get_by_role("region", name="Settings", exact=True).get_by_role(
            "link", name="Contract", exact=True
        ).click()
    page.wait_for_function("document.querySelector('#workspace-content').contentDocument === null")
    page.unroute(destination)
    open_primary_navigation(page)
    page.get_by_role("region", name="Settings", exact=True).get_by_role(
        "link", name="Contract", exact=True
    ).click()
    expect(content.get_by_role("heading", name="Contract settings", exact=True)).to_be_visible()
    expect(page.locator('.sidebar [aria-current="page"]')).to_have_text("Contract")
    assert page_errors == []


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_workspace_hash_views_and_cancelled_navigation_keep_state(app_page):
    """Planning/Savings share a document and cancelled dirty-page navigation retains the menu and draft."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/workspace?page=%2Foverview%23savings")
    content = page.frame_locator("#workspace-content")
    expect(content.locator("#savings-view")).to_be_visible()
    expect(page.locator('.sidebar [aria-current="page"]')).to_have_text("Savings")
    page.locator("#workspace-content").evaluate(
        "element => element.contentWindow.planningMarker = 'same document'"
    )
    open_primary_navigation(page)
    page.get_by_role("link", name="Planning", exact=True).click()
    expect(content.locator("#planning-view")).to_be_visible()
    assert (
        page.locator("#workspace-content").evaluate(
            "element => element.contentWindow.planningMarker"
        )
        == "same document"
    )
    open_primary_navigation(page)
    page.get_by_role("region", name="Settings", exact=True).get_by_role(
        "link", name="Installation", exact=True
    ).click()
    expect(content.locator("#installation-shell")).to_be_visible()
    content.get_by_role("button", name="Continue", exact=True).click()
    content.get_by_label("Maximum grid import (kW)", exact=True).fill("6.3")
    open_primary_navigation(page)
    dialogs = []

    def keep_draft(dialog):
        """Dismiss the browser's actual content-document unload prompt."""
        dialogs.append(dialog.type)
        dialog.dismiss()

    page.once("dialog", keep_draft)
    page.get_by_role("region", name="Settings", exact=True).get_by_role(
        "link", name="Contract", exact=True
    ).click()
    expect(content.get_by_label("Maximum grid import (kW)", exact=True)).to_have_value("6.3")
    expect(page.locator('.sidebar [aria-current="page"]')).to_have_text("Installation")
    assert dialogs == ["beforeunload"]


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_grouped_navigation_is_consistent_accessible_with_courtyard_theme(app_page):
    """Retain grouped destinations and close mobile navigation when resizing through ingress.

    Escape restores Menu focus on mobile. Crossing 640 to 641 pixels clears its
    open state, so returning to mobile hides the sidebar until Menu is activated.
    """
    page, url = app_page
    groups = {
        "Energy": ["Planning", "Appliances", "EV charging", "Thermal storage", "Savings"],
        "Insights": ["Forecast quality", "Planning performance", "Activity"],
        "Settings": ["Installation", "Appliances", "Contract", "Data storage"],
    }
    destinations = (
        ("/overview#planning", "Planning"),
        ("/overview#savings", "Savings"),
        ("/appliances", "Appliances"),
        ("/appliance-settings", "Appliances"),
        ("/ev", "EV charging"),
        ("/thermal", "Thermal storage"),
        ("/quality", "Forecast quality"),
        ("/planning-performance", "Planning performance"),
        ("/activity", "Activity"),
        ("/contract", "Contract"),
        ("/setup", "Installation"),
        ("/data-storage", "Data storage"),
    )
    routes = {label: route for route, label in destinations if route != "/appliance-settings"}
    titles = {
        "/appliance-settings": "Appliances settings",
        "/contract": "Contract settings",
        "/setup": "Installation settings",
    }
    for route, active in destinations:
        page.goto(url + route)
        open_primary_navigation(page)
        navigation = page.get_by_role("navigation", name="Primary navigation")
        for heading, labels in groups.items():
            group = navigation.get_by_role("region", name=heading, exact=True)
            expect(group.get_by_role("heading", name=heading, exact=True)).to_be_visible()
            expect(group.get_by_role("link")).to_have_text(labels)
            for label in labels:
                link = group.get_by_role("link", name=label, exact=True)
                target = (
                    "/appliance-settings"
                    if heading == "Settings" and label == "Appliances"
                    else routes[label]
                )
                expect(link).to_have_attribute("href", urlsplit(url).path + target)
                expect(link).to_be_visible()
                expect(link.locator("svg")).to_have_attribute("aria-hidden", "true")
                expect(link.locator("svg")).to_have_attribute("focusable", "false")
        expected_title = (
            "Set up Open Energy Planner"
            if route == "/setup" and urlsplit(url).path
            else titles.get(route, active)
        )
        expect(page.get_by_role("heading", level=1)).to_have_text(expected_title)
        heading_icon = page.locator("h1 .page-icon")
        assert heading_icon.evaluate("element => element.complete && element.naturalWidth > 0")
        expect(navigation.locator('[aria-current="page"]')).to_have_text(active)
        assert (
            page.locator(".sidebar").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            )
            == "rgb(205, 233, 253)"
        )
        assert (
            navigation.locator('[aria-current="page"]').evaluate(
                "element => getComputedStyle(element).backgroundColor"
            )
            == "rgb(245, 251, 255)"
        )
        logo = page.locator(".brand-mark img")
        expect(logo).to_have_attribute("src", urlsplit(url).path + "/setup/icons/app.svg")
        assert logo.evaluate("element => element.complete && element.naturalWidth > 0")
        page.evaluate("document.fonts.ready")
        assert page.evaluate("document.fonts.check('16px Outfit')")
        hover_link = navigation.get_by_role("link", name="Planning", exact=True)
        hover_link.evaluate("element => element.blur()")
        bounds = hover_link.bounding_box()
        hover_link.hover()
        expect(hover_link).to_have_css("outline-style", "solid")
        expect(hover_link).to_have_css("outline-width", "1px")
        assert hover_link.bounding_box() == bounds
        hover_link.press("Tab")
        hover_link.focus()
        expect(hover_link).to_have_css("outline-width", "3px")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if page.viewport_size["width"] <= 640:
            hover_link.press("Escape")
            expect(navigation).to_be_hidden()
            expect(page.get_by_role("button", name="Menu", exact=True)).to_be_focused()
            mobile_size = page.viewport_size
            menu = page.get_by_role("button", name="Menu", exact=True, include_hidden=True)
            menu.click()
            expect(menu).to_have_attribute("aria-expanded", "true")
            page.set_viewport_size({"width": 641, "height": mobile_size["height"]})
            expect(menu).to_be_hidden()
            expect(menu).to_have_attribute("aria-expanded", "false")
            page.keyboard.press("Escape")
            expect(menu).not_to_be_focused()
            page.set_viewport_size({"width": 640, "height": mobile_size["height"]})
            expect(menu).to_be_visible()
            expect(navigation).to_be_hidden()
            expect(menu).to_have_attribute("aria-expanded", "false")
            page.set_viewport_size(mobile_size)
            if route.startswith("/overview#"):
                open_primary_navigation(page)
                destination = "Savings" if active == "Planning" else "Planning"
                navigation.get_by_role("link", name=destination, exact=True).click()
                expect(navigation).to_be_hidden()
                expect(menu).to_have_attribute("aria-expanded", "false")
                expect(menu).to_be_focused()


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
@pytest.mark.parametrize("shiftable", [False, True])
def test_appliance_workspace_filters_permission_and_handles_empty_refresh(app_page, shiftable):
    """Only permitted members get controls. Known profiles remain readable without permission, and empty membership clears charts."""
    page, url = app_page
    devices = [
        {
            "entity_id": entity,
            "name": name,
            "shiftable": permission,
            "profile": {"power_w": [1200, 0], "samples": 1, "evidence": "synthetic"},
            "task": None,
        }
        for entity, name, permission in (
            ("sensor.dishwasher", "Dishwasher", shiftable),
            ("sensor.heat", "Heat pump", False),
            ("sensor.wallbox", "Wallbox", True),
            ("sensor.removed", "Removed device", True),
        )
    ]
    consumers = {
        "thermal_meters": [],
        "charging_meters": ["sensor.wallbox"],
        "selected": [{"entity_id": device["entity_id"]} for device in devices[:3]],
        "options": [
            {"entity_id": device["entity_id"], "name": device["name"]} for device in devices
        ],
    }
    page.route("**/v1/consumers", lambda route: route.fulfill(json=consumers))
    page.route(
        "**/v1/appliances", lambda route: route.fulfill(json={"devices": devices, "issue": None})
    )
    page.goto(url + "/appliances")
    if "/api/hassio_ingress/" not in url:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Connected")
    expect(page.get_by_label("Finish before (Home Assistant time)")).to_have_count(
        1 if shiftable else 0
    )
    expect(page.locator("#profile-device option")).to_have_text(["Dishwasher", "Heat pump"])
    if shiftable:
        expect(page.locator("#appliance-empty")).to_be_hidden()
    else:
        expect(page.locator("#appliance-empty")).to_be_visible()
    for name in ("Heat pump", "Wallbox", "Removed device"):
        expect(page.get_by_role("heading", name=name, exact=True)).to_have_count(0)

    devices[0]["shiftable"] = False
    page.get_by_role("button", name="Refresh task evidence", exact=True).click()
    expect(page.get_by_role("heading", name="No shiftable appliances", exact=True)).to_be_visible()
    expect(page.locator("#appliance-list")).to_be_empty()
    expect(page.get_by_label("Finish before (Home Assistant time)")).to_have_count(0)
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_have_count(0)
    expect(page.locator("#appliance-profiles")).to_be_visible()
    expect(page.locator("#profile-device option")).to_have_text(["Dishwasher", "Heat pump"])
    original_members = consumers["selected"]
    consumers["selected"] = []
    page.get_by_role("button", name="Refresh task evidence", exact=True).click()
    expect(page.locator("#appliance-profiles")).to_be_hidden()
    assert page.locator("#cycle-chart").evaluate("canvas => !Chart.getChart(canvas)")
    assert page.locator("#cycle-energy-chart").evaluate("canvas => !Chart.getChart(canvas)")
    prefix = urlsplit(url).path
    expect(
        page.locator("#appliance-empty").get_by_role("link", name="Appliances settings")
    ).to_have_attribute("href", prefix + "/appliance-settings")
    expect(
        page.locator("#appliance-empty").get_by_role("link", name="Forecast quality")
    ).to_have_attribute("href", prefix + "/quality")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    consumers["selected"] = original_members
    devices[0]["shiftable"] = True
    page.get_by_role("button", name="Refresh task evidence", exact=True).click()
    expect(page.get_by_role("heading", name="Dishwasher", exact=True)).to_be_visible()
    expect(page.locator("#appliance-empty")).to_be_hidden()
    expect(page.locator("#appliance-profiles")).to_be_visible()
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_be_enabled()
    expect(page.locator("#profile-device option")).to_have_text(["Dishwasher", "Heat pump"])
    page.wait_for_function(
        "() => ['cycle-chart', 'cycle-energy-chart'].every(id => "
        "Boolean(Chart.getChart(document.getElementById(id))))"
    )
    page.goto(url + "/appliance-settings")
    if "/api/hassio_ingress/" not in url:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    heat_pump = page.get_by_role("group", name="Heat pump", exact=True)
    page.locator("#appliance-select").select_option("sensor.heat")
    expect(heat_pump).to_be_visible()
    expect(heat_pump.get_by_label("Allow shifting complete cycles", exact=True)).not_to_be_checked()


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_interactive_planning_navigation_legends_range_and_commands(app_page):
    """Desktop/mobile can inspect actual commands, toggle a series, share a time range and switch focused views."""
    page, url = app_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#planning-view")).to_be_visible()
    expect(page.locator("#savings-view")).to_be_hidden()
    expect(page.locator("#plan-chart")).to_be_visible()
    expect(page.locator("#inspect-values")).to_contain_text("fixed power")
    assert (
        page.evaluate("Chart.getChart(document.getElementById('forecast-chart')).scales.y.min") == 0
    )
    scenario_axes = "['reference-chart','self-consumption-chart','plan-chart'].map(id => {const axis=Chart.getChart(document.getElementById(id)).scales.y;return [axis.min,axis.max];})"
    scenario_bounds = page.evaluate(scenario_axes)
    assert all(lower < upper for lower, upper in scenario_bounds)
    page.locator("#plan-chart-legend input").first.uncheck()
    scenario_bounds = page.evaluate(scenario_axes)
    assert all(lower < upper for lower, upper in scenario_bounds)
    assert (
        page.evaluate("Chart.getChart(document.getElementById('plan-chart')).isDatasetVisible(0)")
        is False
    )
    page.locator("#plan-chart-legend input").first.check()
    page.get_by_role("button", name="Next 6 hours", exact=True).click()
    scenario_bounds = page.evaluate(scenario_axes)
    assert all(lower < upper for lower, upper in scenario_bounds)
    assert page.evaluate(
        "['plan-chart','reference-chart','self-consumption-chart','forecast-chart','energy-chart','price-chart'].every(id => {const x=Chart.getChart(document.getElementById(id)).options.scales.x;return x.max-x.min===21600000;})"
    )
    page.get_by_role("button", name="Next 24 hours", exact=True).click()
    assert page.evaluate(
        "['plan-chart','reference-chart','self-consumption-chart','forecast-chart','energy-chart','price-chart'].every(id => {const x=Chart.getChart(document.getElementById(id)).options.scales.x;return x.max-x.min===86400000;})"
    )
    expect(page.locator("#self-consumption-chart")).to_be_visible()
    assert page.evaluate(
        "['plan-chart','reference-chart','self-consumption-chart','forecast-chart','price-chart'].every(id => {const x=Chart.getChart(document.getElementById(id)).options.scales.x;return x.max-x.min===86400000;})"
    )
    assert page.evaluate(
        "['plan-chart','reference-chart','self-consumption-chart','forecast-chart','energy-chart','price-chart'].every(id => Chart.getChart(document.getElementById(id)).scales.x.ticks.every(tick => tick.value % 900000 === 0))"
    )
    page.get_by_role("button", name="Full horizon", exact=True).click()
    expect(page.get_by_role("button", name="Full horizon", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    assert page.evaluate(
        "['reference-chart','self-consumption-chart','forecast-chart','energy-chart','price-chart'].every(id => {const reference=Chart.getChart(document.getElementById('plan-chart')).scales.x;const scale=Chart.getChart(document.getElementById(id)).scales.x;return scale.min===reference.min && scale.max===reference.max;})"
    )
    page.get_by_text("Understand this plan", exact=True).click()
    page.get_by_text("Selected interval details", exact=True).click()
    page.get_by_label("Inspect planning time", exact=True).focus()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_label("Inspect planning time", exact=True)).to_have_value("1")
    open_primary_navigation(page)
    page.get_by_role("link", name="Savings", exact=True).click()
    expect(page.locator("#planning-view")).to_be_hidden()
    expect(page.locator("#savings-view")).to_be_visible()
    open_primary_navigation(page)
    page.get_by_role("link", name="Planning", exact=True).click()
    expect(page.locator("#planning-view")).to_be_visible()
    expect(page.locator("#savings-view")).to_be_hidden()
    expect(page.get_by_label("Inspect planning time", exact=True)).to_have_value("1")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors == []


def test_first_run_auxiliary_views_retain_draft_and_keyboard_focus(app_page):
    """Diagnostics and strategy never save installation drafts. Return navigation and periodic refresh retain focus and values."""
    page, url = app_page
    page.clock.install()
    discover(page, url)
    connect(page)
    page.get_by_label("Maximum grid import (kW)", exact=True).fill("6")
    page.get_by_label("Maximum grid export (kW)", exact=True).fill("4")
    page.get_by_label("Annual whole-home consumption (kWh)", exact=True).fill("3500")
    home = page.get_by_role("button", name="Home Assistant", exact=True)
    home.focus()
    home.press("Enter")
    expect(home).to_be_focused()
    with page.expect_response(
        lambda response: response.url.endswith("/v1/strategy") and response.request.method == "GET"
    ):
        page.clock.fast_forward(31000)
    expect(home).to_be_focused()
    page.get_by_role("button", name="Price sources", exact=True).click()
    expect(page.get_by_role("button", name="Save installation", exact=True)).to_be_hidden()
    page.get_by_role("button", name="Back to setup", exact=True).click()
    page.get_by_role("button", name="Planning strategy", exact=True).click()
    page.get_by_role("button", name="Edit installation", exact=True).click()
    page.get_by_label("Optimization goal", exact=True).select_option("minimize consumption")
    page.get_by_role("button", name="Save planning strategy", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text(
        "Review the installation draft"
    )
    assert read_settings(page, url)["installation"] is None
    page.get_by_role("button", name="Edit installation", exact=True).click()
    page.get_by_label("Optimization goal", exact=True).select_option("minimize cost")
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_label("Optimization goal", exact=True)).to_have_value("minimize consumption")
    page.get_by_role("button", name="Back to setup", exact=True).click()
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_label("Maximum grid import (kW)", exact=True)).to_have_value("6")
    expect(page.get_by_label("Maximum grid export (kW)", exact=True)).to_have_value("4")
    assert read_settings(page, url)["installation"] is None


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_appliance_settings_categories_share_navigation_and_operational_links(app_page):
    """All device editors remain under Settings, including through the HA ingress prefix."""
    page, url = app_page
    page.goto(url + "/appliance-settings")
    for label, suffix, monitor in (
        ("Electric boilers", "/thermal", "/thermal"),
        ("EV charging", "/ev", "/ev"),
        ("Consumption and cycles", "", "/appliances"),
    ):
        categories = page.get_by_role("navigation", name="Appliance settings", exact=True)
        categories.get_by_role("link", name=label, exact=True).click()
        expect(page).to_have_url(url + "/appliance-settings" + suffix)
        expect(categories.get_by_role("link", name=label, exact=True)).to_have_attribute(
            "aria-current", "page"
        )
        open_primary_navigation(page)
        settings = page.get_by_role("region", name="Settings", exact=True)
        expect(settings.get_by_role("link", name="Appliances", exact=True)).to_have_attribute(
            "aria-current", "page"
        )
        if page.get_by_role("button", name="Menu", exact=True).is_visible():
            page.get_by_role("button", name="Menu", exact=True).click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.goto(url + monitor)
        if suffix:
            expect(
                page.get_by_role("link", name="Configure", exact=False).filter(
                    has_text="Appliances settings"
                )
            ).to_have_count(1)
        page.goto(url + "/appliance-settings" + suffix)


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_wiki_help_opens_external_guide_without_leaving_app(app_page):
    """Desktop, mobile and HA ingress open the public wiki separately from the current app page."""
    page, url = app_page
    open_primary_navigation(page)
    link = page.get_by_role("link", name="Wiki help", exact=True)
    link.scroll_into_view_if_needed()
    expect(link).to_be_visible()
    expect(link).to_have_attribute("href", "https://patrickvorgers.github.io/open-energy-planner/")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    page.context.route(
        "https://patrickvorgers.github.io/open-energy-planner/",
        lambda route: route.fulfill(body="<h1>Wiki destination</h1>", content_type="text/html"),
    )
    page.screenshot(
        path=f"results/ci/wiki-help-{page.viewport_size['width']}-{'ingress' in url}.png",
        full_page=True,
    )
    with page.expect_popup() as opened:
        link.click()
    popup = opened.value
    expect(popup).to_have_url("https://patrickvorgers.github.io/open-energy-planner/")
    expect(page).to_have_url(url + "/setup")
    popup.close()
