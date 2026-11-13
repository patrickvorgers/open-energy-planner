"""Responsive app-owned setup against real authenticated API/storage, without physical devices."""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    TOKEN,
    assert_floating_save_bar,
    connect,
    discover,
    edit_strategy,
    finish,
    grid,
    installation_area,
    physical_plan,
    read_settings,
)


def _discovered_installation_devices(page, url, kind, count):
    """Provide distinct Energy-owned meters and complete battery metadata without physical hardware."""
    body = discover(page, url, publish=False)
    hints = body["suggestions"]

    def entity(identity, unit, device_class):
        """Add independently named synthetic metadata using the actual catalogue wire shape."""
        body["entities"].append(
            {
                "entity_id": identity,
                "name": identity,
                "device_id": "",
                "unit": unit,
                "device_class": device_class,
                "state_class": "total_increasing" if device_class == "energy" else "measurement",
            }
        )

    entity("sensor.outgoing", "kWh", "energy")
    hints["grid_export"] = "sensor.outgoing"
    hints["terms"].append({"entity_id": "sensor.outgoing", "sign": -1})
    for index in range(count):
        if kind == "solar":
            identity = f"sensor.solar_{index:02}"
            entity(identity, "kWh", "energy")
            hints["solar_sources"].append(identity)
            hints["terms"].append({"entity_id": identity, "sign": 1})
            hints["solar_configurations"].append(
                {
                    "entity_id": identity,
                    "providers": [],
                    "issue": "solar_configuration_not_available",
                }
            )
            hints["solar"] = True
        else:
            battery = {"capacity_kwh": 10}
            for field, unit, device_class in (
                ("energy_entity", "kWh", "energy"),
                ("charge_energy_entity", "kWh", "energy"),
                ("soc_entity", "%", "battery"),
                ("power_entity", "W", "power"),
            ):
                identity = f"sensor.battery_{index:02}_{field}"
                entity(identity, unit, device_class)
                battery[field] = identity
            hints["batteries"].append(battery)
            hints["soc"].append(battery["soc_entity"])
            hints["terms"].extend(
                {"entity_id": battery[field], "sign": sign}
                for field, sign in (("energy_entity", 1), ("charge_energy_entity", -1))
            )
    response = page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": "Bearer " + TOKEN}, data=body
    )
    assert response.ok, f"Synthetic catalogue request failed (HTTP {response.status})"
    return body


def test_grid_feed_in_permission_uses_the_persisted_export_limit(app_page):
    """Desktop/mobile save zero export, restore it on reload and require a confirmed limit when enabling again."""
    page, url = app_page
    discover(page, url)
    connect(page)
    permission = page.get_by_label("Allow grid feed-in", exact=True)
    export = page.get_by_label("Maximum grid export (kW)", exact=True)
    expect(permission).to_be_checked()
    expect(export).to_be_enabled()
    expect(page.locator("#connection-examples")).to_contain_text("17.25 kW")
    expect(page.locator("#connection-examples")).to_contain_text("8.05 kW")
    page.get_by_label("Maximum grid import (kW)", exact=True).fill("5")
    page.get_by_label("Annual whole-home consumption (kWh)", exact=True).fill("3500")
    export.fill("5")
    permission.uncheck()
    expect(export).to_be_disabled()
    expect(export).to_have_value("0")
    expect(page.locator("#feed-in-warning")).to_be_visible()
    expect(page.locator("#feed-in-warning")).to_contain_text("no feasible plan")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    screenshots = Path("results/ci/installation-export")
    screenshots.mkdir(parents=True, exist_ok=True)

    def capture(name, selector):
        """Capture the actual settings card without a scrolled mobile header covering its controls."""
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(
            path=str(screenshots / f"{name}-{page.viewport_size['width']}.png"),
            full_page=True,
            clip=page.locator(selector).bounding_box(),
        )

    capture("disabled", '[data-step="0"]')
    page.get_by_role("button", name="Continue", exact=True).click()
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    assert read_settings(page, url)["installation"]["export_limit_w"] == 0
    expect(page.locator("#installation-capacities")).to_contain_text("0 kW")
    expect(page.locator("#installation-summary-content")).to_contain_text("Allow grid feed-inOff")
    expect(page.locator("#installation-summary-content").get_by_role("note")).to_contain_text(
        "no feasible plan"
    )
    capture("saved", "#installation-summary")
    page.reload()
    connect(page)
    installation_area(page, 0, edit=True)
    expect(permission).not_to_be_checked()
    expect(export).to_be_disabled()
    permission.check()
    expect(export).to_be_enabled()
    expect(export).to_have_value("")
    assert export.evaluate("element => element.validity.valueMissing")
    before = read_settings(page, url)
    writes = []

    def record_configuration_write(request):
        """Observe real preview and configuration requests without replacing the API."""
        if request.method in ("POST", "PUT") and urlsplit(request.url).path in (
            "/v1/setup/preview",
            "/v1/installation",
            "/v1/solar",
            "/v1/demand",
        ):
            writes.append(request.url)

    page.on("request", record_configuration_write)
    save = page.get_by_role("button", name="Save installation", exact=True)
    expect(save).to_be_enabled()
    save.click()
    page.evaluate("() => window.__oepProductReadsIdle()")
    assert writes == []
    after = read_settings(page, url)
    for section in ("installation", "solar", "demand"):
        assert after[section] == before[section]
    expect(permission).to_be_checked()
    expect(export).to_be_enabled()
    expect(export).to_have_value("")
    assert export.evaluate("element => element.validity.valueMissing")
    expect(page.locator("#installation-save-state")).to_contain_text(
        "Complete the highlighted fields"
    )
    export.fill("2.5")
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    assert read_settings(page, url)["installation"]["export_limit_w"] == 2500
    installation_area(page, 0, edit=True)
    permission.uncheck()
    permission.check()
    expect(export).to_have_value("2.5")
    permission.uncheck()
    page.get_by_role("button", name="Cancel", exact=True).click()
    installation_area(page, 0, edit=True)
    expect(permission).to_be_checked()
    expect(export).to_have_value("2.5")
    capture("enabled", '[data-step="0"]')


def test_explicit_zero_export_explains_effective_no_feed_in(app_page):
    """A manually entered zero warns immediately and in the saved summary, then reloads as feed-in off."""
    page, url = app_page
    discover(page, url)
    connect(page)
    page.get_by_label("Maximum grid import (kW)", exact=True).fill("5")
    page.get_by_label("Annual whole-home consumption (kWh)", exact=True).fill("3500")
    export = page.get_by_label("Maximum grid export (kW)", exact=True)
    expect(export).to_have_value("")
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_role("heading", name="Connection and household", exact=True)).to_be_visible()
    assert export.evaluate("element => element.validity.valueMissing")
    export.fill("5")
    page.get_by_role("button", name="Continue", exact=True).click()
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    installation_area(page, 0, edit=True)
    permission = page.get_by_label("Allow grid feed-in", exact=True)
    expect(permission).to_be_checked()
    export.fill("0")
    expect(permission).to_be_checked()
    expect(page.locator("#feed-in-warning")).to_be_visible()
    expect(page.locator("#feed-in-warning")).to_contain_text("no feasible plan")
    screenshots = Path("results/ci/installation-export")
    screenshots.mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=str(screenshots / f"manual-zero-{page.viewport_size['width']}.png"),
        full_page=True,
        clip=page.locator('[data-step="0"]').bounding_box(),
    )
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    assert read_settings(page, url)["installation"]["export_limit_w"] == 0
    expect(page.locator("#installation-summary-content")).to_contain_text("Allow grid feed-inOff")
    expect(page.locator("#installation-summary-content").get_by_role("note")).to_contain_text(
        "no feasible plan"
    )
    page.reload()
    connect(page)
    installation_area(page, 0, edit=True)
    expect(permission).not_to_be_checked()
    expect(export).to_be_disabled()
    expect(export).to_have_value("0")


@pytest.mark.parametrize("outcome", ["success", "rejected", "uncertain", "strategy-failed"])
def test_saved_zero_export_ends_the_unsaved_limit_cache(app_page, outcome):
    """Only a confirmed zero Installation write ends draft restoration, even if the later strategy write fails."""
    page, url = app_page
    discover(page, url)
    connect(page)
    page.get_by_label("Maximum grid import (kW)", exact=True).fill("5")
    page.get_by_label("Annual whole-home consumption (kWh)", exact=True).fill("3500")
    export = page.get_by_label("Maximum grid export (kW)", exact=True)
    permission = page.get_by_label("Allow grid feed-in", exact=True)
    export.fill("5")
    permission.uncheck()
    for _ in range(3):
        page.get_by_role("button", name="Continue", exact=True).click()
    if outcome == "strategy-failed":
        page.locator("#strategy").select_option("minimize consumption")
        page.route("**/v1/strategy", lambda route: route.fulfill(status=503))
    elif outcome in ("rejected", "uncertain"):

        def fail_installation_response(route):
            """Optionally commit the real write before losing its acknowledgement."""
            if outcome == "uncertain":
                assert route.fetch().ok
            route.fulfill(status=503)

        page.route("**/v1/installation", fail_installation_response)
    page.get_by_role("button", name="Save installation", exact=True).click()
    confirmed = outcome in ("success", "strategy-failed")
    if outcome == "success":
        expect(page.locator("#installation-save-state")).to_contain_text("Installation saved.")
        installation_area(page, 0, edit=True)
    else:
        expect(page.locator("#installation-save-state")).to_contain_text("Your edits are retained")
        page.get_by_role("button", name="Connection", exact=True).click()
    saved = read_settings(page, url)
    if outcome == "rejected":
        assert saved["installation"] is None
    else:
        assert saved["installation"]["export_limit_w"] == 0
    permission.check()
    expect(export).to_have_value("" if confirmed else "5")
    expect(export).to_be_enabled()
    if confirmed:
        assert export.evaluate("element => element.validity.valueMissing")
        if outcome == "success":
            writes = []
            page.on(
                "request",
                lambda request: (
                    writes.append(request.url) if request.method in ("POST", "PUT") else None
                ),
            )
            page.get_by_role("button", name="Save installation", exact=True).click()
            page.evaluate("() => window.__oepProductReadsIdle()")
            assert writes == []
            assert read_settings(page, url)["installation"] == saved["installation"]
            export.fill("2.5")
            page.get_by_role("button", name="Save installation", exact=True).click()
            expect(page.locator("#installation-save-state")).to_contain_text("Installation saved.")
            assert read_settings(page, url)["installation"]["export_limit_w"] == 2500


def test_battery_calibration_is_opt_in_and_survives_explicit_save(app_page):
    """Desktop/mobile can enable defaults, save a different interval and discard an unsaved disable."""
    page, url = app_page
    _discovered_installation_devices(page, url, "battery", 1)
    connect(page)
    grid(page)
    page.get_by_role("button", name="Continue", exact=True).click()
    editor = page.locator("#battery-rows > fieldset").first
    editor.get_by_label("Maximum charge power (kW)", exact=True).fill("2")
    editor.get_by_label("Maximum discharge power (kW)", exact=True).fill("2")
    editor.get_by_label("Own inverter AC rating (kW)", exact=True).fill("3")
    toggle = editor.get_by_label("Periodic battery calibration", exact=True)
    expect(toggle).not_to_be_checked()
    expect(editor.get_by_label("Calibration interval (days)", exact=True)).not_to_be_visible()
    toggle.check()
    expect(editor.get_by_label("Calibration interval (days)", exact=True)).to_have_value("14")
    expect(editor.get_by_label("Calibration hold (hours)", exact=True)).to_have_value("3")
    interval = editor.get_by_label("Calibration interval (days)", exact=True)
    interval.fill("181")
    assert interval.evaluate("element => element.validity.rangeOverflow")
    interval.fill("180")
    editor.get_by_label("Calibration hold (hours)", exact=True).fill("2.5")
    toggle.uncheck()
    toggle.check()
    expect(interval).to_have_value("180")
    expect(editor.get_by_label("Calibration hold (hours)", exact=True)).to_have_value("2.5")
    finish(page)
    saved = read_settings(page, url)["installation"]["batteries"][0]
    assert saved["calibration_enabled"] is True
    assert saved["calibration_interval_days"] == 180
    assert saved["calibration_hold_hours"] == 2.5
    page.reload()
    connect(page)
    installation_area(page, 2, edit=True)
    expect(toggle).to_be_checked()
    expect(editor.get_by_label("Calibration interval (days)", exact=True)).to_have_value("180")
    screenshots = Path("results/ci/battery-calibration")
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(screenshots / f"installation-{page.viewport_size['width']}.png"), full_page=True
    )
    toggle.evaluate("element => element.closest('label').scrollIntoView({block: 'start'})")
    page.evaluate("window.scrollBy(0, -130)")
    page.screenshot(path=str(screenshots / f"calibration-{page.viewport_size['width']}.png"))
    toggle.uncheck()
    page.get_by_role("button", name="Cancel", exact=True).click()
    installation_area(page, 2, edit=True)
    expect(toggle).to_be_checked()
    toggle.uncheck()
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    disabled = read_settings(page, url)["installation"]["batteries"][0]
    assert disabled["calibration_enabled"] is False
    assert disabled["calibration_interval_days"] == 180
    assert disabled["calibration_hold_hours"] == 2.5


@pytest.mark.parametrize("app_site", ["calibration_overdue_hold"], indirect=True)
def test_overdue_calibration_shows_active_hold_and_deadline(app_page):
    """Desktop/mobile retain both overdue and active-hold guidance from a real restored ledger."""
    page, url = app_page
    assert read_settings(page, url)["installation"]["batteries"][0]["calibration_enabled"] is True
    _discovered_installation_devices(page, url, "battery", 1)
    assert read_settings(page, url)["installation"]["batteries"][0]["calibration_enabled"] is True
    connect(page)
    installation_area(page, 2)
    status = page.locator(".calibration-status:visible")
    expect(status).to_contain_text("Overdue since")
    expect(status).to_contain_text("Holding until")
    expect(status).to_contain_text("valid measurements are still required")
    status.scroll_into_view_if_needed()
    screenshots = Path("results/ci/calibration-overdue-hold")
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(screenshots / f"calibration-overdue-{page.viewport_size['width']}.png")
    )


@pytest.mark.parametrize(
    "calibration_field_label,invalid_draft",
    [("Calibration interval (days)", ""), ("Calibration hold (hours)", "25")],
)
def test_disabled_calibration_can_save_after_invalid_enabled_draft(
    app_page, calibration_field_label, invalid_draft
):
    """An invalid calibration draft must not block native Save after opting out, including after reload."""
    page, url = app_page
    _discovered_installation_devices(page, url, "battery", 1)
    connect(page)
    grid(page)
    page.get_by_role("button", name="Continue", exact=True).click()
    editor = page.locator("#battery-rows > fieldset").first
    editor.get_by_label("Maximum charge power (kW)", exact=True).fill("2")
    editor.get_by_label("Maximum discharge power (kW)", exact=True).fill("2")
    editor.get_by_label("Own inverter AC rating (kW)", exact=True).fill("3")
    editor.get_by_label("Periodic battery calibration", exact=True).check()
    finish(page)
    installation_area(page, 2, edit=True)
    toggle = editor.get_by_label("Periodic battery calibration", exact=True)
    toggle.check()
    invalid = editor.get_by_label(calibration_field_label, exact=True)
    invalid.fill(invalid_draft)
    assert not invalid.evaluate("element => element.checkValidity()")
    toggle.uncheck()
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    saved = read_settings(page, url)["installation"]["batteries"][0]
    assert saved.get("calibration_enabled", False) is False
    page.reload()
    connect(page)
    installation_area(page, 2, edit=True)
    expect(toggle).not_to_be_checked()
    toggle.check()
    expect(editor.get_by_label("Calibration interval (days)", exact=True)).to_have_value("14")
    expect(editor.get_by_label("Calibration hold (hours)", exact=True)).to_have_value("3")


@pytest.mark.parametrize("kind", ["solar", "battery"])
def test_seventeen_installation_devices_can_be_saved_reloaded_and_edited(app_page, kind):
    """Every one of seventeen Energy sources survives setup and a later edit of the final device."""
    from copy import deepcopy

    page, url = app_page
    _discovered_installation_devices(page, url, kind, 17)
    connect(page)
    grid(page)
    if kind == "solar":
        selector = page.locator("#installation-solar")
        expect(selector.locator("option")).to_have_count(17)
        for index in range(17):
            selector.select_option(str(index))
            source = page.locator("#solar-rows > fieldset").nth(index)
            source.get_by_label("Panel capacity (kWp)", exact=True).fill("1")
            source.get_by_label("Inverter AC rating (kW)", exact=True).fill("3")
    page.get_by_role("button", name="Continue", exact=True).click()
    if kind == "battery":
        selector = page.locator("#installation-battery")
        expect(selector.locator("option")).to_have_count(17)
        for index in range(17):
            selector.select_option(str(index))
            battery = page.locator("#battery-rows > fieldset").nth(index)
            battery.get_by_label("Maximum charge power (kW)", exact=True).fill("2")
            battery.get_by_label("Maximum discharge power (kW)", exact=True).fill("2")
            battery.get_by_label("Own inverter AC rating (kW)", exact=True).fill("3")
    finish(page)
    saved = read_settings(page, url)
    assert (
        len(saved["solar"]["sources"] if kind == "solar" else saved["installation"]["batteries"])
        == 17
    )
    page.reload()
    connect(page)
    installation_area(page, 1 if kind == "solar" else 2, edit=True)
    selector = page.locator("#installation-solar" if kind == "solar" else "#installation-battery")
    expect(selector.locator("option")).to_have_count(17)
    selector.select_option("16")
    if kind == "solar":
        page.locator("#solar-rows > fieldset").nth(16).get_by_label(
            "Inverter AC rating (kW)", exact=True
        ).fill("3.5")
    else:
        page.locator("#battery-rows > fieldset").nth(16).get_by_label(
            "Maximum charge power (kW)", exact=True
        ).fill("2.25")
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    edited = read_settings(page, url)
    if kind == "solar":
        assert edited["solar"]["sources"] == saved["solar"]["sources"]
        expected = deepcopy(saved["solar"]["inverters"])
        identity = saved["solar"]["sources"][16]["inverter_id"]
        next(inverter for inverter in expected if inverter["inverter_id"] == identity)[
            "ac_limit_w"
        ] = 3500
        assert edited["solar"]["inverters"] == expected
    else:
        expected = deepcopy(saved["installation"]["batteries"])
        expected[16]["charge_limit_w"] = 2250
        assert edited["installation"]["batteries"] == expected


def test_seventeenth_manual_panel_group_can_be_added_saved_and_reloaded(app_page):
    """Seventeen independently identified planes retain geometry after creation, reload and editing."""
    page, url = app_page
    _discovered_installation_devices(page, url, "solar", 1)
    connect(page)
    grid(page)
    source = page.locator("#solar-rows > fieldset").first
    source.get_by_label("Panel capacity (kWp)", exact=True).fill("0.1")
    source.get_by_label("Inverter AC rating (kW)", exact=True).fill("3")
    for index in range(1, 17):
        source.get_by_role("button", name="Add panel group", exact=True).click()
        group = source.locator(".solar-panel").nth(index)
        group.locator('[data-key="panel_name"]').fill(f"Plane {index + 1}")
        group.locator('[data-key="panel_capacity"]').fill("0.1")
        group.locator('[data-key="panel_azimuth"]').fill("180")
        group.locator('[data-key="panel_tilt"]').fill("35")
    expect(source.locator(".solar-panel")).to_have_count(17)
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    saved = read_settings(page, url)["solar"]["sources"][0]["panels"]
    assert len(saved) == 17
    assert len({panel["panel_id"] for panel in saved}) == 17
    assert sum(panel["capacity_w"] for panel in saved) == 1700
    page.reload()
    connect(page)
    installation_area(page, 1, edit=True)
    expect(source.locator(".solar-panel")).to_have_count(17)
    source.locator(".solar-panel").nth(16).locator('[data-key="panel_tilt"]').fill("40")
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    edited = read_settings(page, url)["solar"]["sources"][0]["panels"]
    assert edited[:16] == saved[:16]
    assert edited[16] == {**saved[16], "tilt": 40}


@pytest.mark.parametrize("available_days", [365, 180])
def test_installation_distinguishes_measured_year_from_extrapolation(app_page, available_days):
    """Desktop/mobile use the available seasonal balance and explain whether its annual total is measured."""
    page, url = app_page
    body = discover(page, url)
    end_day = int(datetime.now(UTC).timestamp()) // 86400
    cumulative_kwh = 100
    points = [[(end_day - available_days) * 86400, cumulative_kwh]]
    for day_index in range(available_days):
        cumulative_kwh += 20 if day_index >= available_days - 28 else 5
        points.append([(end_day - available_days + day_index + 1) * 86400, cumulative_kwh])
    body["history"] = [{"entity_id": "sensor.incoming", "points": [points[0], points[-1]]}]
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    ).ok
    connect(page)
    expected_kwh = round(((available_days - 28) * 5 + 28 * 20) / available_days * 365, 1)
    expect(page.locator("#annual")).to_have_js_property("valueAsNumber", expected_kwh)
    page.get_by_role("button", name="Home Assistant", exact=True).click()
    summary = page.locator("#ha-installation-facts")
    if available_days == 365:
        expect(summary).to_contain_text("Measured over 365 complete days")
        expect(summary).not_to_contain_text("Extrapolated")
    else:
        expect(summary).to_contain_text("Extrapolated from 180 complete days within the last year")
        expect(summary).to_contain_text("Seasonal differences may affect it")
    summary.scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    screenshots = Path("results/ci/annual-setup")
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(screenshots / f"{available_days}-days-{page.viewport_size['width']}.png"),
        full_page=True,
    )


def test_energy_names_refresh_without_discarding_source_drafts(app_page):
    """Desktop/mobile receive Energy renames automatically while source selection and unsaved ratings remain intact."""
    page, url = app_page
    page.clock.install()
    metadata = discover(page, url, devices=True)
    for entry in metadata["entities"]:
        if entry["entity_id"] in ("sensor.roof", "sensor.shed"):
            entry.update(
                device_name="Shared inverter",
                energy_name="South roof" if entry["entity_id"] == "sensor.roof" else "Garden roof",
            )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=metadata).ok
    connect(page)
    grid(page)
    selector = page.locator("#installation-solar")
    expect(selector.locator("option").first).to_contain_text("South roof")
    selector.select_option("1")
    draft = (
        page.locator("#solar-rows > fieldset")
        .nth(1)
        .get_by_label("Panel capacity (kWp)", exact=True)
    )
    draft.fill("4.25")
    original = read_settings(page, url)
    for entry in metadata["entities"]:
        if entry["entity_id"] == "sensor.shed":
            entry["energy_name"] = "Garden extension"
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=metadata).ok
    page.clock.fast_forward(31000)
    expect(selector.locator("option").nth(1)).to_contain_text("Garden extension")
    expect(selector).to_have_value("1")
    expect(draft).to_have_value("4.25")
    current = read_settings(page, url)
    assert current["installation"] == original["installation"]
    assert current["solar"] == original["solar"]
    assert [row["id"] for row in current["devices"]] == [row["id"] for row in original["devices"]]
    image = Path("results/ci") / f"energy-source-names-{page.viewport_size['width']}.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(image), full_page=True)


def test_hybrid_converter_name_refresh_keeps_battery_draft(app_page):
    """A solar Energy rename updates a selected hybrid connection without changing its ID or battery ratings."""
    page, url = app_page
    page.clock.install()
    metadata = discover(page, url, devices=True)
    connect(page)
    grid(page)
    for index in range(2):
        page.locator("#installation-solar").select_option(str(index))
        source = page.locator("#solar-rows > fieldset").nth(index)
        source.get_by_label("Panel capacity (kWp)", exact=True).fill("3")
        source.get_by_text("Shared inverter settings", exact=True).click()
        source.get_by_label("Inverter ID", exact=True).fill(f"converter{index}")
        source.get_by_label("Inverter AC rating (kW)", exact=True).fill("3")
        if index == 0:
            source.get_by_label("Hybrid inverter with battery connection", exact=True).check()
    page.get_by_role("button", name="Continue", exact=True).click()
    battery = page.locator("#battery-rows > fieldset").first
    connection = battery.get_by_label("Converter connection", exact=True)
    connection.select_option("converter0")
    battery.get_by_label("Maximum charge power (kW)", exact=True).fill("2.35")
    for row in metadata["entities"]:
        if row["entity_id"] == "sensor.roof":
            row["energy_name"] = "South extension"
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": "Bearer " + TOKEN}, data=metadata
    ).ok
    page.clock.fast_forward(31000)
    expect(connection.locator("option:checked")).to_contain_text("South extension")
    expect(connection).to_have_value("converter0")
    expect(battery.get_by_label("Maximum charge power (kW)", exact=True)).to_have_value("2.35")


def test_unnamed_solar_model_sources_keep_distinct_fallback_labels(app_page):
    """The model selector distinguishes unnamed sources while preserving an explicit source named Solar."""
    page, _url = app_page
    labels = page.evaluate("""async () => {
        const {currentModelRows} = await import('/setup/quality-models.js');
        const solar = {configuration: {sources: [{source_id: 'one'}, {source_id: 'two'}]}};
        const selection = {models: [{scope: 'solar:one', label: 'Solar: old source one'}, {scope: 'solar:two', label: 'Solar: old source two'}]};
        const fallback = [...currentModelRows({solar}, selection).values()].map(row => row.label);
        solar.source_names = {one: 'Solar', two: 'South roof'};
        return {fallback, explicit: [...currentModelRows({solar}, selection).values()].map(row => row.label)};
    }""")
    assert labels == {"fallback": ["Solar 1", "Solar 2"], "explicit": ["Solar", "South roof"]}


def test_source_name_watcher_retries_a_superseded_refresh(app_page):
    """A superseded refresh cannot consume the revision. The next poll retries once and retains a successful revision."""
    page, _url = app_page
    page.clock.install()
    page.evaluate("""async () => {
        const {watchSourceNames} = await import('/setup/display.js');
        window.nameRefreshes = 0;
        watchSourceNames(async () => ({source_names_revision: 'new'}), async () => {
            window.nameRefreshes++;
            return window.nameRefreshes > 1 ? true : undefined;
        }, 'old');
    }""")
    assert page.evaluate("window.nameRefreshes") == 0
    page.clock.fast_forward(31000)
    page.wait_for_function("window.nameRefreshes === 1")
    page.clock.fast_forward(31000)
    page.wait_for_function("window.nameRefreshes === 2")
    page.clock.fast_forward(31000)
    assert page.evaluate("window.nameRefreshes") == 2


def test_manual_strategy_choice_is_persistent_and_explains_stock_correction(app_page):
    """Desktop/mobile save a real strategy, reload it and see that replacement kWh are accounting rather than commands."""
    page, url = app_page
    page.clock.install()
    page.goto(url + "/setup")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    edit_strategy(page)
    expect(page.get_by_label("Optimization goal", exact=True)).to_have_value("minimize cost")
    assert_floating_save_bar(page)
    page.get_by_text("Compare optimization goals", exact=True).click()
    expect(page.locator("#strategy-help")).to_contain_text("depleted starting battery stock")
    page.get_by_label("Optimization goal", exact=True).select_option("minimize consumption")
    page.get_by_role("button", name="Save planning strategy", exact=True).click()
    expect(page.locator("#strategy-status")).to_be_empty()
    edit_strategy(page)
    page.get_by_label("Optimization goal", exact=True).select_option("minimize cost")
    with page.expect_response(
        lambda response: response.url.endswith("/v1/strategy") and response.request.method == "GET"
    ):
        page.clock.fast_forward(31000)
    expect(page.get_by_label("Optimization goal", exact=True)).to_have_value("minimize cost")
    expect(page.locator("#strategy-status")).to_be_empty()
    page.reload()
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    edit_strategy(page)
    expect(page.get_by_label("Optimization goal", exact=True)).to_have_value("minimize consumption")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize(
    "app_page",
    [{"width": 1280, "height": 900}, {"width": 768, "height": 1024}, {"width": 390, "height": 844}],
    indirect=True,
)
def test_strategy_summary_matches_saved_fields_and_retains_edit_save_cancel(app_page):
    """Saved goals use ordinary field rows while comparison, draft cancellation and persistence work at each viewport."""
    page, url = app_page
    discover(page, url)
    connect(page)
    grid(page)
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    reference = page.locator("#installation-summary dd").first.evaluate(
        "element => ({size: getComputedStyle(element).fontSize, weight: getComputedStyle(element).fontWeight})"
    )
    installation_area(page, 4)
    summary = page.locator("#installation-strategy-value")
    expect(summary.locator("dt")).to_have_text("Optimization goal")
    expect(summary.locator("dd")).to_have_text("Minimize cost")
    expect(page.get_by_label("Optimization goal", exact=True)).to_be_hidden()
    expect(page.locator("#strategy-explanation")).to_contain_text("lowest expected economic cost")
    assert (
        summary.locator("dd").evaluate(
            "element => ({size: getComputedStyle(element).fontSize, weight: getComputedStyle(element).fontWeight})"
        )
        == reference
    )
    label_bounds = summary.locator("dt").bounding_box()
    value_bounds = summary.locator("dd").bounding_box()
    assert value_bounds["x"] >= label_bounds["x"] + label_bounds["width"]
    assert (
        page.get_by_role("heading", name="Planning strategy", exact=True).bounding_box()["y"]
        < label_bounds["y"]
    )
    expect(page.locator("#strategy-help")).to_be_hidden()
    comparison = page.get_by_text("Compare optimization goals", exact=True)
    comparison.click()
    expect(page.locator("#strategy-help")).to_contain_text("Mandatory reserve recovery comes first")
    expect(page.locator("#strategy-help")).to_contain_text(
        "Minimize cost chooses the lowest expected economic cost"
    )
    expect(page.locator("#strategy-help")).to_contain_text("Export does not offset import")
    comparison.click()
    expect(page.locator("#strategy-help")).to_be_hidden()
    expect(page.get_by_role("status", name="Strategy status")).to_be_empty()
    screenshots = Path("results/ci")
    screenshots.mkdir(parents=True, exist_ok=True)
    width = page.viewport_size["width"]
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=str(screenshots / f"installation-strategy-saved-{width}.png"), full_page=True
    )
    edit_strategy(page)
    expect(summary).to_be_hidden()
    page.get_by_label("Optimization goal", exact=True).select_option("minimize consumption")
    expect(page.locator("#strategy-explanation")).to_have_text(
        "Reduce grid import plus the grid energy needed to replace depleted starting battery stock."
    )
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=str(screenshots / f"installation-strategy-edit-{width}.png"), full_page=True
    )
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(summary.locator("dd")).to_have_text("Minimize cost")
    expect(page.locator("#strategy-explanation")).to_contain_text("lowest expected economic cost")
    edit_strategy(page)
    page.get_by_label("Optimization goal", exact=True).select_option("minimize consumption")
    page.route(
        "**/v1/strategy",
        lambda route: (
            route.fulfill(status=503, body="Strategy storage unavailable")
            if route.request.method == "PUT"
            else route.continue_()
        ),
    )
    page.get_by_role("button", name="Save planning strategy", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
    expect(summary).to_be_hidden()
    expect(page.get_by_label("Optimization goal", exact=True)).to_have_value("minimize consumption")
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=str(screenshots / f"installation-strategy-error-{width}.png"), full_page=True
    )
    page.unroute("**/v1/strategy")
    page.get_by_role("button", name="Save planning strategy", exact=True).click()
    expect(summary.locator("dd")).to_have_text("Minimize consumption")
    page.reload()
    connect(page)
    installation_area(page, 4)
    expect(summary.locator("dd")).to_have_text("Minimize consumption")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_strategy_status_failure_does_not_block_setup_or_retry(app_page):
    """A temporary strategy read failure keeps Energy setup usable and recovers the save baseline on retry."""
    page, url = app_page
    page.clock.install()
    page.route(
        "**/v1/strategy",
        lambda route: route.fulfill(status=503, body="Archive temporarily unavailable"),
    )
    page.goto(url + "/setup")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Waiting for Home Assistant")
    page.get_by_role("button", name="Planning strategy", exact=True).click()
    expect(page.get_by_role("status", name="Strategy status")).to_contain_text("unavailable")
    expect(page.locator("#save")).to_be_disabled()
    page.unroute("**/v1/strategy")
    with page.expect_response(
        lambda response: response.url.endswith("/v1/strategy") and response.request.method == "GET"
    ):
        page.clock.fast_forward(31000)
    expect(page.locator("#save")).to_be_disabled()
    page.get_by_role("button", name="Edit installation", exact=True).click()
    page.get_by_label("Optimization goal", exact=True).select_option("minimize consumption")
    expect(page.get_by_role("button", name="Save planning strategy", exact=True)).to_be_enabled()
    expect(page.get_by_role("status", name="Strategy status")).to_be_empty()


def test_missing_energy_capacity_directs_user_to_home_assistant(app_page):
    """An Energy battery without capacity cannot fall back to a manually editable OEP value."""
    page, url = app_page
    body = discover(page, url, devices=True)
    body["suggestions"]["batteries"][0]["capacity_kwh"] = None
    response = page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    )
    assert response.ok
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Settings > Dashboards > Energy")
    expect(page.locator("#status")).to_contain_text("capacity kwh")
    expect(page.get_by_role("button", name="Continue", exact=True)).to_be_disabled()
    assert read_settings(page, url)["installation"] is None


def test_installation_partial_save_retains_draft_and_retries_its_own_saved_revision(app_page):
    """A solar-write failure after demand persistence retains the draft, locks editing in flight and retries safely."""
    page, url = app_page
    discover(page, url)
    connect(page)
    grid(page)
    page.get_by_role("button", name="Continue", exact=True).click()
    page.get_by_role("button", name="Continue", exact=True).click()
    pending = []
    page.route("**/v1/solar", lambda route: pending.append(route))
    with page.expect_request(
        lambda request: request.url.endswith("/v1/solar") and request.method == "PUT"
    ):
        page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-bar")).to_have_attribute("data-save-state", "saving")
    expect(page.locator("#annual")).to_be_disabled()
    expect(page.locator("#discard-installation")).to_be_disabled()
    pending[0].fulfill(status=503, body="Solar storage unavailable")
    expect(page.locator("#installation-save-state")).to_contain_text("Your edits are retained")
    expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
    expect(page.locator("#annual")).to_have_value("3500")
    expect(page.locator("#annual")).to_be_enabled()
    partial = read_settings(page, url)
    assert partial["demand"]["version"] and partial["solar"] is None
    assert_floating_save_bar(page)
    for identity in ("grid", "export-energy", "grid-invert"):
        expect(page.locator("#" + identity)).to_be_disabled()
    page.unroute("**/v1/solar")
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    saved = read_settings(page, url)
    assert saved["demand"]["version"] == partial["demand"]["version"]
    assert saved["installation"]["version"] and saved["solar"]["version"]


def test_grid_only_app_wizard_keeps_optional_devices_absent(app_page):
    """Saving Energy-configured/grid-only setup creates no PV/battery sources, is responsive and can be reopened without changing its version."""
    page, url = app_page
    discover(page, url)
    connect(page)
    grid(page)
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    saved = read_settings(page, url)
    assert saved["installation"]["batteries"] == []
    assert saved["solar"]["sources"] == []
    assert saved["demand"]["terms"] == [{"entity_id": "sensor.incoming", "sign": 1}]
    assert saved["demand"]["annual_kwh"] == 3500
    assert saved["demand"]["timezone"] == "Europe/Amsterdam"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert (
        page.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length") == 0
    )
    page.reload()
    connect(page)
    for identity in ("grid", "export-energy", "grid-invert"):
        expect(page.locator("#" + identity)).to_be_disabled()
    assert read_settings(page, url)["installation"]["version"] == saved["installation"]["version"]
    page.goto(url + "/")
    expect(
        page.frame_locator("#workspace-content").get_by_role(
            "heading", name="Original plan", exact=True
        )
    ).to_be_visible()
    page.reload()
    expect(
        page.frame_locator("#workspace-content").get_by_role(
            "heading", name="Original plan", exact=True
        )
    ).to_be_visible()
    expect(page.frame_locator("#workspace-content").locator("#planning-view")).to_be_visible()
    expect(page.frame_locator("#workspace-content").locator("#savings-view")).to_be_hidden()
    overview_navigations = []
    page.on(
        "framenavigated",
        lambda frame: (
            overview_navigations.append(frame.url)
            if urlsplit(frame.url).path.endswith("/overview")
            else None
        ),
    )
    page.goto(url + "/#savings")
    expect(page.frame_locator("#workspace-content").locator("#savings-view")).to_be_visible()
    expect(page.locator('.sidebar [aria-current="page"]')).to_have_text("Savings")
    assert page.evaluate("new URL(location.href).searchParams.get('page')") == "/overview#savings"
    assert overview_navigations[0].endswith("#savings")
    page.goto(url + "/workspace?page=%2Foverview%23planning#savings")
    expect(page.frame_locator("#workspace-content").locator("#planning-view")).to_be_visible()
    expect(page.locator('.sidebar [aria-current="page"]')).to_have_text("Planning")


@pytest.mark.parametrize("wear_rate", ["0.025", "0.004"])
def test_app_wizard_preserves_multiple_converters_and_hybrid_battery(app_page, wear_rate):
    """Converter identities, exact wear rates and SoC edits survive wizard steps. Clearing minimum SoC stays invalid and cannot replace saved 10.04 with zero."""
    page, url = app_page
    discover(page, url, devices=True)
    connect(page)
    grid(page)
    for index, (name, hybrid) in enumerate((("roof", True), ("shed", False))):
        page.locator("#installation-solar").select_option(str(index))
        item = page.locator("#solar-rows > fieldset").nth(index)
        expect(item.get_by_label("Production energy sensor", exact=True)).to_be_disabled()
        item.get_by_label("Panel capacity (kWp)", exact=True).fill("3")
        item.get_by_text("Shared inverter settings", exact=True).click()
        item.get_by_label("Inverter ID", exact=True).fill(name)
        item.get_by_label("Inverter AC rating (kW)", exact=True).fill("3")
        if hybrid:
            item.get_by_label("Hybrid inverter with battery connection", exact=True).check()
    page.get_by_role("button", name="Continue", exact=True).click()
    for index in range(2):
        page.locator("#installation-battery").select_option(str(index))
        item = page.locator("#battery-rows > fieldset").nth(index)
        expect(item.get_by_label("State of charge sensor", exact=True)).to_be_hidden()
        options = (
            item.get_by_label("State of charge sensor", exact=True)
            .locator("option")
            .evaluate_all("items => items.map(i => i.value)")
        )
        assert "sensor.soc0" in options and "sensor.wrong_soc" not in options
        assert item.get_by_label("Usable capacity (kWh)").get_attribute("aria-describedby")
        expect(item.get_by_label("Actual battery power sensor", exact=True)).to_be_hidden()
        expect(item.get_by_label("Usable capacity (kWh)", exact=True)).to_be_disabled()
        expect(item.get_by_label("Usable capacity (kWh)", exact=True)).to_have_value("10")
        expect(item.get_by_label("Battery charge energy meter", exact=True)).to_be_disabled()
        item.get_by_label("Maximum charge power (kW)", exact=True).fill("2")
        item.get_by_label("Maximum discharge power (kW)", exact=True).fill("2")
        if index == 0:
            item.get_by_label("Cost per kWh (optional)", exact=True).fill(wear_rate)
            item.get_by_label("Minimum state of charge (%)", exact=True).fill("10.03")
            item.get_by_label("Converter connection", exact=True).select_option("roof")
            item.get_by_label("Can follow zero grid target", exact=True).check()
            item.get_by_label("Restore minimum reserve immediately", exact=True).check()
            assert item.get_by_label(
                "Restore minimum reserve immediately", exact=True
            ).get_attribute("aria-describedby")
    finish(page)
    before = read_settings(page, url)
    assert len(before["installation"]["batteries"]) == 2
    assert before["installation"]["batteries"][0]["inverter_id"] == "roof"
    assert before["installation"]["batteries"][0]["recover_reserve"] is True
    assert before["installation"]["batteries"][1].get("recover_reserve", False) is False
    assert before["installation"]["batteries"][0]["can_follow_grid"] is True
    assert before["installation"]["batteries"][1].get("can_follow_grid", False) is False
    assert before["installation"]["batteries"][1]["inverter_id"] != "roof"
    assert before["installation"]["batteries"][0]["minimum_soc"] == 10.03
    assert before["installation"]["batteries"][0]["wear_cost_per_kwh"] == float(wear_rate)
    installation_area(page, 2)
    page.locator("#installation-battery").select_option("0")
    expect(
        page.locator("#installation-summary dt", has_text="Cost per kWh")
        .locator("..")
        .locator("dd")
    ).to_have_text(wear_rate)
    page.locator("#installation-summary").screenshot(
        path=f"results/ci/battery-wear-precision/saved-{wear_rate}-{page.viewport_size['width']}.png"
    )
    installation_area(page, 2)
    page.locator("#installation-battery").select_option("1")
    expect(page.locator("#installation-summary")).to_contain_text("sensor.discharge1")
    expect(
        page.locator("#installation-summary dt", has_text="Cost per kWh")
        .locator("..")
        .locator("dd")
    ).to_have_text("0.00")
    page.get_by_role("button", name="Edit installation", exact=True).click()
    page.locator("#installation-battery").select_option("0")
    minimum = page.get_by_label("Minimum state of charge (%)", exact=True).first
    expect(minimum).to_have_value("10.0")
    expect(page.get_by_label("Cost per kWh (optional)", exact=True).first).to_have_value(wear_rate)
    minimum.fill("10.04")
    minimum.press("Tab")
    expect(minimum).to_have_value("10.0")
    expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
    expect(page.get_by_role("button", name="Save installation", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    before = read_settings(page, url)
    assert before["installation"]["batteries"][0]["minimum_soc"] == 10.04
    installation_area(page, 2, edit=True)
    page.locator("#installation-battery").select_option("0")
    minimum = page.get_by_label("Minimum state of charge (%)", exact=True).first
    minimum.fill("")
    minimum.press("Tab")
    with page.expect_request(
        lambda request: request.url.endswith("/v1/setup/preview") and request.method == "POST"
    ) as preview:
        page.get_by_role("button", name="Save installation", exact=True).click()
    assert preview.value.post_data_json["installation"]["batteries"][0]["minimum_soc"] is None
    expect(page.locator("#installation-save-state")).to_contain_text("Your edits are retained")
    assert read_settings(page, url)["installation"]["batteries"][0]["minimum_soc"] == 10.04
    minimum.fill("10.04")
    minimum.press("Tab")
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_role("button", name="Edit installation", exact=True)).to_be_visible()
    expect(page.locator("#installation-area")).to_have_value("2")
    page.locator("#installation-battery").select_option("1")
    page.get_by_role("button", name="Edit installation", exact=True).click()
    page.locator("#battery-rows > fieldset").nth(1).get_by_label(
        "Maximum charge power (kW)", exact=True
    ).fill("3")
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_role("button", name="Edit installation", exact=True)).to_be_visible()
    expect(page.locator("#installation-battery")).to_have_value("1")
    assert read_settings(page, url)["installation"] == before["installation"]
    installation_area(page, 1, edit=True)
    page.locator("#installation-solar").select_option("0")
    item = page.locator("#solar-rows > fieldset").first
    item.get_by_label("Panel capacity (kWp)", exact=True).fill("4")
    item.get_by_label("Allow proposed solar on/off commands", exact=True).check()
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    after = read_settings(page, url)
    assert after["solar"]["sources"][0]["source_id"] == before["solar"]["sources"][0]["source_id"]
    assert after["solar"]["sources"][0]["capacity_w"] == 4000
    assert after["solar"]["sources"][0]["can_curtail"] is True
    assert after["installation"]["batteries"] == before["installation"]["batteries"]
    # Energy membership changes are reflected without a second add/remove editor.
    reduced = discover(page, url, devices=True)
    reduced["suggestions"]["solar_sources"] = ["sensor.roof"]
    reduced["suggestions"]["solar_configurations"] = [
        reduced["suggestions"]["solar_configurations"][0]
    ]
    reduced["suggestions"]["batteries"] = reduced["suggestions"]["batteries"][:1]
    reduced["suggestions"]["terms"] = [
        energy_term
        for energy_term in reduced["suggestions"]["terms"]
        if energy_term["entity_id"] not in ("sensor.shed", "sensor.discharge1", "sensor.charge1")
    ]
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=reduced
    ).ok
    page.reload()
    connect(page)
    installation_area(page, -1)
    expect(page.locator("#ha-installation-summary")).to_contain_text("excluded automatically")
    expect(page.get_by_role("button", name="Add solar source", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Add battery", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Remove solar source", exact=True)).to_have_count(0)
    installation_area(page, 1)
    expect(page.locator("#solar-rows > fieldset")).to_have_count(1)
    expect(page.get_by_label("Production energy sensor", exact=True)).to_be_disabled()
    installation_area(page, 2)
    expect(page.locator("#battery-rows > fieldset")).to_have_count(1)
    expect(page.get_by_label("Energy dashboard battery", exact=True)).to_be_disabled()
    # Energy synchronization persists removals without a redundant manual save.
    expect(page.locator("#save")).to_be_disabled()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    retained = read_settings(page, url)
    assert len(retained["solar"]["sources"]) == 1
    assert len(retained["installation"]["batteries"]) == 1
    assert retained["solar"]["sources"][0]["source_id"] == after["solar"]["sources"][0]["source_id"]
    # A removed hybrid creates an unconfirmed converter draft. Saving strategy must not persist it.
    reduced["suggestions"]["solar_sources"] = []
    reduced["suggestions"]["solar_configurations"] = []
    reduced["suggestions"]["solar"] = False
    reduced["suggestions"]["terms"] = [
        energy_term
        for energy_term in reduced["suggestions"]["terms"]
        if energy_term["entity_id"] != "sensor.roof"
    ]
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=reduced
    ).ok
    page.reload()
    connect(page)
    installation_area(page, 2)
    expect(page.get_by_label("Converter connection", exact=True)).to_have_value("standalone")
    expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
    before_strategy = read_settings(page, url)
    installation_area(page, 4, edit=True)
    expect(page.get_by_role("button", name="Save planning strategy", exact=True)).to_be_disabled()
    page.get_by_label("Optimization goal", exact=True).select_option("minimize consumption")
    put_request_paths = []
    page.on(
        "request",
        lambda request: (
            put_request_paths.append(urlsplit(request.url).path)
            if request.method == "PUT"
            else None
        ),
    )
    page.get_by_role("button", name="Save planning strategy", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("Planning strategy saved")
    assert put_request_paths == ["/v1/strategy"]
    after_strategy = read_settings(page, url)
    assert after_strategy["installation"] == before_strategy["installation"]
    assert after_strategy["demand"]["version"] == before_strategy["demand"]["version"]
    assert after_strategy["solar"]["version"] == before_strategy["solar"]["version"]
    installation_area(page, 2)
    expect(page.get_by_label("Converter connection", exact=True)).to_have_value("standalone")
    expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")


@pytest.mark.parametrize("status", ["missing", "incomplete"])
def test_app_requires_complete_energy_dashboard_and_recovers(app_page, status):
    """Missing/incomplete Energy configuration blocks the wizard. Refreshing a corrected dashboard unblocks it without losing authentication."""
    page, url = app_page
    discover(page, url, status=status)
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Energy dashboard")
    expect(page.get_by_role("button", name="Continue", exact=True)).to_be_disabled()
    discover(page, url)
    page.get_by_role("button", name="Refresh Home Assistant data").click()
    expect(page.locator("#status")).to_contain_text("Refreshing Home Assistant configuration")
    discover(page, url)  # The connected integration answers the explicit refresh request.
    expect(page.locator("#progress")).to_have_text("Stage 1 of 5", timeout=10000)


def test_history_prefills_known_inputs_and_sensor_dropdowns_are_typed(app_page):
    """Seven complete 10 kWh days imply 3650 kWh/year. Detected values are prefilled, and dropdowns exclude incompatible device classes."""
    page, url = app_page
    discover(page, url, history=True)
    connect(page)
    expect(page.get_by_label("Annual whole-home consumption (kWh)")).to_have_value("3650")
    expect(page.get_by_label("Grid power sensor", exact=True)).to_be_disabled()
    expect(page.locator("#annual")).to_have_js_property("valueAsNumber", 3650)
    options = page.locator("#grid option").evaluate_all("items => items.map(i => i.value)")
    assert (
        "sensor.grid" in options
        and "sensor.temperature" not in options
        and "sensor.wrong_power" not in options
    )
    assert page.get_by_label("Maximum grid import (kW)").get_attribute("aria-describedby")
    grid(page)
    expect(page.get_by_role("button", name="Add solar source", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_role("button", name="Add battery", exact=True)).to_have_count(0)
    finish(page)
    assert read_settings(page, url)["demand"]["annual_kwh"] == 3650


def test_configured_grid_and_solar_remain_visible_without_measurements_or_history(app_page):
    """Dashboard-selected PV remains selected without live metadata/history. No absent battery is invented and annual scale stays a separate question."""
    page, url = app_page
    body = discover(page, url)
    body["suggestions"].update(solar=True, solar_sources=["sensor.roof"])
    body["suggestions"]["solar_configurations"] = [
        {"entity_id": "sensor.roof", "providers": [], "issue": "solar_configuration_not_available"}
    ]
    body["suggestions"]["terms"].append({"entity_id": "sensor.roof", "sign": 1})
    body["entities"] = [
        entity for entity in body["entities"] if entity["entity_id"] != "sensor.roof"
    ]
    response = page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    )
    assert response.ok
    connect(page)
    page.get_by_role("button", name="Home Assistant", exact=True).click()
    expect(page.locator("#ha-installation-facts")).to_contain_text("sensor.roof")
    expect(page.locator("#ha-installation-facts")).to_contain_text("BatteriesNone configured")
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_label("Grid power sensor", exact=True)).to_be_disabled()
    expect(page.get_by_label("Annual whole-home consumption (kWh)", exact=True)).to_be_visible()
    grid(page)
    sensor = page.get_by_label("Production energy sensor", exact=True)
    expect(sensor).to_have_value("sensor.roof")
    expect(sensor.locator("option:checked")).to_contain_text("Missing or incompatible")
    expect(page.locator("#battery-rows > fieldset")).to_have_count(0)


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_converter_gap_is_explained_without_hiding_complete_installation_balance(app_page):
    """Four DC kWh equal 1 load + 1.4 export + 1.6 charge, but the archived 2-kWh converter output is inconsistent."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    identity = page.request.get(url + "/v1/plans?limit=20", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    body = page.request.get(url + f"/v1/plans/{identity}/comparison", headers=headers).json()
    fixture = physical_plan()
    problem = fixture["input_snapshot"]["problem"]
    problem["intervals"] = problem["intervals"][:1]
    problem["inverters"][0]["efficiency"] = 1
    problem["batteries"][0]["dc_efficiency"] = 1
    fixture["expected_flows"] = fixture["expected_flows"][:1]
    fixture["expected_flows"][0]["grid_export"] = 1.4
    fixture["expected_flows"][0]["inverters"][0]["export_kwh"] = 2
    body["plan"]["input_snapshot"]["problem"] = problem
    body["plan"]["expected_flows"] = fixture["expected_flows"]
    body["comparison"], body["scenarios"] = None, {}
    page.route("**/v1/plans/*/comparison?view=plan", lambda route: route.fulfill(json=body))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#plan-chart-legend .balance-warning")).to_have_count(0)
    assert page.evaluate(
        "Chart.getChart(document.getElementById('plan-chart')).data.datasets.filter(row => row.type === 'bar').every(row => Number.isFinite(row.data[0].y))"
    )
    page.get_by_text("Understand this plan", exact=True).click()
    page.get_by_text("Selected interval details", exact=True).click()
    expect(page.locator("#inspect-values")).to_contain_text(
        "Battery converter 1 (1 interval(s) with unavailable physical evidence)"
    )
    expect(page.locator("[id^=converter-balance]")).to_have_count(0)


def test_shared_money_and_installation_timezone_ignore_browser_clock(app_page):
    """Desktop/mobile format all money to two decimals and resolve HA-local dates across DST independently of the browser."""
    page, url = app_page
    page.goto(url + "/contract")
    result = page.evaluate("""async () => {
        const display = await import('/setup/display.js');
        display.useHomeAssistantTimezone('Europe/Amsterdam');
        let gapRejected = false;
        try { display.localInstant('2026-03-29T02:30'); } catch { gapRejected = true; }
        return {money: [0, 1, -1.234, null].map(display.money),
            winter: display.localInstant('2026-01-01'), summer: display.localInstant('2026-07-01'),
            fold: display.localInstant('2026-10-25T02:30'), gapRejected,
            localEditors: [null, undefined, 0, '2026-01-01T00:12:34Z', '2026-06-30T22:30:00Z'].map(display.localDateTime),
            day: display.localDate('2026-06-30T22:30:00Z'),
            label: display.optionLabel('contract_anniversary'),
            minute: display.when('2026-10-25T12:34:56.789Z'),
            zone: display.when('2026-10-25T12:34:56.789Z', {timeZoneName: 'short'}),
            tick: display.when('2026-10-25T12:34:56.789Z', {hour: '2-digit', minute: '2-digit', hour12: false}),
            exact: display.localInstant('2026-10-25T13:34:56'),
            dateStyle: display.when('2026-10-25T12:34:56.789Z', {dateStyle: 'short'}),
            timeStyles: ['short', 'medium', 'long', 'full'].map(timeStyle => display.when('2026-10-25T12:34:56.789Z', {dateStyle: 'short', timeStyle})),
            timezoneOverride: display.when('2026-10-25T12:34:56.789Z', {hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC'})};
    }""")
    assert result == {
        "money": ["0.00", "1.00", "-1.23", "-"],
        "winter": "2025-12-31T23:00:00.000Z",
        "summer": "2026-06-30T22:00:00.000Z",
        "fold": "2026-10-25T00:30:00.000Z",
        "gapRejected": True,
        "localEditors": ["", "", "1970-01-01T01:00", "2026-01-01T01:12", "2026-07-01T00:30"],
        "day": "2026-07-01",
        "label": "Contract anniversary",
        "minute": "10/25/2026, 1:34 PM",
        "zone": "10/25/2026, 1:34 PM GMT+1",
        "tick": "13:34",
        "exact": "2026-10-25T12:34:56.000Z",
        "dateStyle": "10/25/26",
        "timeStyles": ["10/25/26, 1:34 PM"] * 4,
        "timezoneOverride": "13:34",
    }


def test_failed_installation_refresh_keeps_existing_fields_editable(app_page):
    """A failed settings read leaves the rendered installation part of the editable save snapshot."""
    page, url = app_page
    discover(page, url)
    connect(page)
    grid(page)
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    page.route(
        "**/v1/setup?refresh=1", lambda route: route.fulfill(status=503, body="Setup unavailable")
    )
    installation_area(page, -1)
    page.get_by_role("button", name="Refresh Home Assistant data", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("Setup unavailable")
    installation_area(page, 0, edit=True)
    page.locator("#annual").fill("3600")
    expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
    expect(page.get_by_role("button", name="Save installation", exact=True)).to_be_enabled()


def test_installation_design_first_run_review_and_readonly_maintenance(app_page):
    """A grid-only first run saves at review. Maintenance Edit/Cancel never persists an abandoned limit."""
    page, url = app_page
    discover(page, url)
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(
        page.get_by_role("heading", name="Set up Open Energy Planner", exact=True)
    ).to_be_visible()
    expect(page.locator("#progress")).to_have_text("Stage 1 of 5")
    page.get_by_role("button", name="Continue", exact=True).click()
    page.get_by_label("Maximum grid import (kW)", exact=True).fill("5")
    page.get_by_label("Maximum grid export (kW)", exact=True).fill("4")
    page.get_by_label("Annual whole-home consumption (kWh)", exact=True).fill("3500")
    for _ in range(3):
        page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_role("heading", name="Review your installation", exact=True)).to_be_visible()
    expect(page.locator("#review-solar")).to_have_text("0 sources: Not configured")
    expect(page.locator("#review-batteries")).to_have_text("0 batteries: Not configured")
    expect(page.locator("#installation-capacities strong")).to_have_text(
        ["5 kW", "4 kW", "Not configured", "Not configured"]
    )
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    expect(page.get_by_role("heading", name="Installation settings", exact=True)).to_be_visible()
    expect(page.locator("#installation-summary")).to_contain_text("Maximum grid import")
    expect(page.locator("#installation-summary")).to_contain_text("3500")
    before = read_settings(page, url)
    page.get_by_role("button", name="Edit installation", exact=True).click()
    page.get_by_label("Maximum grid import (kW)", exact=True).fill("6")
    expect(page.locator("#installation-area")).to_be_disabled()
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_role("button", name="Edit installation", exact=True)).to_be_visible()
    assert read_settings(page, url)["installation"] == before["installation"]
    expect(page.locator("#import")).to_have_value("5")
    expect(page.locator("#import")).to_be_disabled()
    if page.viewport_size["width"] > 700:
        page.set_viewport_size({"width": 1440, "height": 900})
    edit_button_bounds = page.get_by_role(
        "button", name="Edit installation", exact=True
    ).bounding_box()
    save_state_bounds = page.locator("#installation-save-state").bounding_box()
    assert save_state_bounds["y"] >= edit_button_bounds["y"] + edit_button_bounds["height"]
    if page.viewport_size["width"] > 700:
        toolbar = page.locator("#installation-toolbar").bounding_box()
        assert (
            toolbar["x"] + toolbar["width"] - edit_button_bounds["x"] - edit_button_bounds["width"]
            <= 32
        )
    page.get_by_role("combobox", name="Installation area", exact=True).select_option("5")
    expect(page.get_by_role("heading", name="Active price sources", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Forecast models", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Edit installation", exact=True)).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_installation_design_reveals_invalid_unselected_source_before_advancing(app_page):
    """With two PV sources, Continue reveals the unconfigured second source and never persists a partial wizard."""
    page, url = app_page
    discover(page, url, devices=True)
    connect(page)
    grid(page)
    first = page.locator("#solar-rows > fieldset").first
    first.get_by_label("Panel capacity (kWp)", exact=True).fill("3")
    first.get_by_label("Inverter AC rating (kW)", exact=True).fill("3")
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.locator("#installation-solar")).to_have_value("1")
    second = page.locator("#solar-rows > fieldset").nth(1)
    expect(second.get_by_label("Panel capacity (kWp)", exact=True)).to_be_visible()
    assert read_settings(page, url)["installation"] is None
    expect(page.get_by_role("button", name="Save installation", exact=True)).to_be_hidden()
    second.get_by_label("Panel capacity (kWp)", exact=True).fill("2")
    second.get_by_label("Inverter AC rating (kW)", exact=True).fill("2")
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_role("heading", name="Batteries", exact=True)).to_be_visible()
    assert read_settings(page, url)["installation"] is None


def test_installation_lists_four_solar_sources_and_all_grid_meters(app_page):
    """HA and connection summaries list every meter. Four independent PV drafts retain their values and IDs."""
    page, url = app_page
    body = discover(page, url, devices=True)
    for name, unit, kind in (
        ("terrace", "kWh", "energy"),
        ("carport", "kWh", "energy"),
        ("incoming2", "kWh", "energy"),
        ("outgoing2", "kWh", "energy"),
        ("grid2", "W", "power"),
    ):
        body["entities"].append(
            {
                "entity_id": "sensor." + name,
                "name": name,
                "unit": unit,
                "device_class": kind,
                "state_class": "total_increasing" if kind == "energy" else "measurement",
                "device_id": "",
            }
        )
    hints = body["suggestions"]
    hints["solar_sources"].extend(["sensor.terrace", "sensor.carport"])
    hints["solar_configurations"].extend(
        {"entity_id": entity, "providers": [], "issue": "solar_configuration_not_available"}
        for entity in ("sensor.terrace", "sensor.carport")
    )
    hints["grid_power_entities"].append("sensor.grid2")
    hints["terms"].extend(
        {"entity_id": "sensor." + name, "sign": sign}
        for name, sign in (("terrace", 1), ("carport", 1), ("incoming2", 1), ("outgoing2", -1))
    )
    hints["settlement"] = {
        "imports": ["sensor.incoming", "sensor.incoming2"],
        "exports": ["sensor.outgoing", "sensor.outgoing2"],
    }
    hints["grid_export"] = "sensor.outgoing"
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    ).ok
    connect(page)
    page.get_by_role("button", name="Home Assistant", exact=True).click()
    for title, expected in (
        ("Solar sources", hints["solar_sources"]),
        ("Grid power sensors", hints["grid_power_entities"]),
        ("Grid import energy meters", hints["settlement"]["imports"]),
        ("Grid export energy meters", hints["settlement"]["exports"]),
    ):
        row = page.locator("#ha-installation-facts > div").filter(
            has=page.locator("dt", has_text=title)
        )
        expect(row.locator("li")).to_have_count(len(expected))
        for entity in expected:
            expect(row).to_contain_text(entity)
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.locator("#installation-grid-sources")).to_contain_text("sensor.incoming2")
    expect(page.locator("#installation-grid-sources")).to_contain_text("sensor.outgoing2")
    grid(page)
    expect(page.locator("#installation-solar option")).to_have_count(4)
    for index in range(4):
        page.locator("#installation-solar").select_option(str(index))
        source = page.locator("#solar-rows > fieldset").nth(index)
        expect(source.get_by_label("Inverter ID", exact=True)).to_be_hidden()
        source.get_by_label("Panel capacity (kWp)", exact=True).fill(str(index + 1))
        source.get_by_label("Inverter AC rating (kW)", exact=True).fill("3")
    page.locator("#installation-solar").select_option("0")
    expect(
        page.locator("#solar-rows > fieldset").first.get_by_label(
            "Panel capacity (kWp)", exact=True
        )
    ).to_have_value("1")
    ids = page.locator('#solar-rows [data-key="inverter_id"]').evaluate_all(
        "items => items.map(input => input.value)"
    )
    assert len(set(ids)) == 4
    expect(page.locator("#installation-capacities")).to_contain_text("Grid export limit")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert read_settings(page, url)["installation"] is None


def test_installation_is_usable_before_strategy_preference_arrives(app_page):
    """A held preference response cannot block setup. Its eventual arrival preserves physical drafts."""
    page, url = app_page
    discover(page, url)
    pending = []
    page.route("**/v1/strategy", lambda route: pending.append(route))
    connect(page)
    expect(page.get_by_label("Maximum grid import (kW)", exact=True)).to_be_visible()
    page.get_by_label("Maximum grid import (kW)", exact=True).fill("7")
    assert pending
    for route in pending:
        route.fulfill(json={"requested": "minimize cost", "issue": None})
    page.unroute("**/v1/strategy")
    page.get_by_role("button", name="Home Assistant", exact=True).click()
    page.get_by_role("button", name="Planning strategy", exact=True).click()
    expect(page.get_by_label("Optimization goal", exact=True)).to_have_value("minimize cost")
    expect(page.locator("#strategy-status")).to_be_empty()
    expect(page.get_by_label("Maximum grid import (kW)", exact=True)).to_have_value("7")


def test_installation_duplicate_solar_names_keep_distinct_hybrid_choices(app_page):
    """Equal HA names retain unique meter labels and exact inverter option values without writing configuration."""
    page, url = app_page
    body = discover(page, url, devices=True)
    for entity in body["entities"]:
        if entity["entity_id"] in ("sensor.roof", "sensor.shed"):
            entity["name"] = "Solar"
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    ).ok
    connect(page)
    grid(page)
    expect(page.locator("#installation-solar option")).to_have_text(
        ["Solar (sensor.roof)", "Solar (sensor.shed)"]
    )
    for index in range(2):
        page.locator("#installation-solar").select_option(str(index))
        source = page.locator("#solar-rows > fieldset").nth(index)
        source.get_by_label("Panel capacity (kWp)", exact=True).fill("3")
        source.get_by_label("Inverter AC rating (kW)", exact=True).fill("3")
        source.get_by_label("Hybrid inverter with battery connection", exact=True).check()
    inverter = (
        page.locator("#solar-rows > fieldset")
        .nth(1)
        .locator('[data-key="inverter_id"]')
        .input_value()
    )
    page.get_by_role("button", name="Continue", exact=True).click()
    connection = page.locator("#battery-rows > fieldset").first.get_by_label(
        "Converter connection", exact=True
    )
    expect(connection.locator("option")).to_have_text(
        ["Own inverter", "Solar (sensor.roof)", "Solar (sensor.shed)"]
    )
    connection.select_option(label="Solar (sensor.shed)")
    expect(connection).to_have_value(inverter)
    assert read_settings(page, url)["installation"] is None


@pytest.mark.parametrize("action", ["cancel", "reload"])
def test_strategy_reload_owns_baseline_during_inflight_price_poll(app_page, action):
    """A delayed price poll cannot supersede explicit reload/discard or confirm a stale strategy baseline."""
    page, url = app_page
    page.clock.install()
    discover(page, url)
    connect(page)
    grid(page)
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    physical_before = read_settings(page, url)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    saved_goal = "minimize consumption"
    if action == "cancel":
        installation_area(page, 0, edit=True)
        page.get_by_label("Maximum grid import (kW)", exact=True).fill("7")
        expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
    else:
        installation_area(page, -1)
    assert page.request.put(
        url + "/v1/strategy",
        headers=headers,
        data={"strategy": saved_goal, "expected_strategy": "minimize cost"},
    ).ok
    prices = page.request.get(url + "/v1/prices", headers=headers).json()
    prices["market_area"] = "overlap-test"
    price_pending = []

    def hold_price(route):
        """Expose completion of the browser route callback without a timing-based sleep."""
        price_pending.append(route)
        page.evaluate("window.heldPriceRequest = true")

    page.route("**/v1/prices", hold_price)
    page.clock.fast_forward(31000)
    page.wait_for_function("() => window.heldPriceRequest === true")
    assert len(price_pending) == 1
    strategy_pending = []

    def preference_response(route):
        """Hold the explicit read while allowing the older price poll's later read to finish."""
        if not strategy_pending:
            strategy_pending.append(route)
            page.evaluate("window.explicitPreferenceHeld = true")
        else:
            route.fulfill(json={"requested": saved_goal, "issue": None})

    page.route("**/v1/strategy", preference_response)
    page.route("**/v1/setup?refresh=1", lambda route: route.fulfill(json=physical_before))
    with page.expect_request("**/v1/strategy"):
        page.get_by_role(
            "button",
            name="Cancel" if action == "cancel" else "Refresh Home Assistant data",
            exact=True,
        ).click()
    page.wait_for_function("() => window.explicitPreferenceHeld === true")
    expect(page.locator("#installation-save-state")).to_contain_text(
        "Discarding changes" if action == "cancel" else "Refreshing settings"
    )
    expect(page.locator("#strategy")).to_be_disabled()
    price_pending[0].fulfill(json=prices)
    expect(page.locator("#price-market")).to_contain_text("overlap-test")
    expect(page.locator("#strategy")).to_be_disabled()
    strategy_pending[0].fulfill(json={"requested": saved_goal, "issue": None})
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    expect(page.locator("#strategy")).to_have_value(saved_goal)
    assert read_settings(page, url)["installation"] == physical_before["installation"]
    assert page.request.get(url + "/v1/strategy", headers=headers).json()["requested"] == saved_goal
    page.unroute("**/v1/strategy")
    page.unroute("**/v1/prices")
    page.unroute("**/v1/setup?refresh=1")
    edit_strategy(page)
    next_goal = "minimize consumption" if saved_goal == "minimize cost" else "minimize cost"
    page.get_by_label("Optimization goal", exact=True).select_option(next_goal)
    with page.expect_request(
        lambda request: request.url.endswith("/v1/strategy") and request.method == "PUT"
    ) as saved_request:
        page.get_by_role("button", name="Save planning strategy", exact=True).click()
    assert saved_request.value.post_data_json == {
        "strategy": next_goal,
        "expected_strategy": saved_goal,
    }
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    assert page.request.get(url + "/v1/strategy", headers=headers).json()["requested"] == next_goal
    with page.expect_response(
        lambda response: response.url.endswith("/v1/strategy") and response.request.method == "GET"
    ):
        page.clock.fast_forward(31000)
    expect(page.locator("#strategy")).to_have_value(next_goal)
    expect(page.locator("#strategy-status")).to_be_empty()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
