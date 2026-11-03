"""Share browser actions and independent setup documents for authenticated app scenarios."""

from datetime import UTC, datetime

from playwright.sync_api import expect

TOKEN = "local-ui-test-only-0123456789abcdef"

DEFAULT_BROWSER_TIME_ZONE = "UTC"

SAVINGS_BROWSER_TIME_ZONE = "America/New_York"

MOBILE_CHART_LEGEND_BREAKPOINT_PX = 640


def open_primary_navigation(page):
    """Open the shared mobile Menu when hidden before choosing a navigation destination."""
    if not page.get_by_role("navigation", name="Primary navigation").is_visible():
        page.get_by_role("button", name="Menu", exact=True).click()


def discover(
    page,
    url,
    *,
    history=False,
    status="ready",
    devices=False,
    country="NL",
    timezone="Europe/Amsterdam",
):
    """Push normalized HA metadata to the actual app. Incompatible sensors must never enter dropdowns."""
    entities = []
    for name, unit, kind, state_class in (
        ("grid", "W", "power", "measurement"),
        ("incoming", "kWh", "energy", "total_increasing"),
        ("roof", "kWh", "energy", "total_increasing"),
        ("shed", "kWh", "energy", "total_increasing"),
        ("soc0", "%", "battery", "measurement"),
        ("soc1", "%", "battery", "measurement"),
        ("battery0", "W", "power", "measurement"),
        ("battery1", "kW", "power", "measurement"),
        ("temperature", "°C", "temperature", "measurement"),
        ("wrong_power", "W", "temperature", "measurement"),
        ("wrong_soc", "%", "humidity", "measurement"),
    ):
        entities.append(
            {
                "entity_id": "sensor." + name,
                "name": name,
                "unit": unit,
                "device_class": kind,
                "state_class": state_class,
                "device_id": "",
            }
        )
    body = {
        "entities": entities,
        "timezone": timezone,
        "country": country,
        "suggestions": {
            "terms": [{"entity_id": "sensor.incoming", "sign": 1}] if status == "ready" else [],
            "grid_power": "sensor.grid",
            "grid_power_entities": ["sensor.grid"],
            "solar": False,
            "solar_sources": [],
            "soc": [],
            "energy_status": status,
            "batteries": [],
            "grid_export": None,
        },
        "history": [
            {
                "entity_id": "sensor.incoming",
                "points": [
                    [
                        (int(datetime.now(UTC).timestamp()) // 86400 - 7 + index_item) * 86400,
                        index_item * 10,
                    ]
                    for index_item in (0, 7)
                ],
            }
        ]
        if history
        else [],
    }
    if devices:
        body["suggestions"]["solar"] = True
        body["suggestions"]["solar_sources"] = ["sensor.roof", "sensor.shed"]
        body["suggestions"]["batteries"] = [
            {
                "energy_entity": f"sensor.discharge{index_item}",
                "charge_energy_entity": f"sensor.charge{index_item}",
                "capacity_kwh": 10,
                "soc_entity": f"sensor.soc{index_item}",
                "power_entity": f"sensor.battery{index_item}",
            }
            for index_item in range(2)
        ]
    if devices:
        for name in ("outgoing", "discharge0", "discharge1", "charge0", "charge1"):
            body["entities"].append(
                {
                    "entity_id": "sensor." + name,
                    "name": name,
                    "unit": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "device_id": "",
                }
            )
        body["suggestions"]["terms"].extend(
            {"entity_id": "sensor." + name, "sign": sign}
            for name, sign in (
                ("outgoing", -1),
                ("roof", 1),
                ("shed", 1),
                ("discharge0", 1),
                ("discharge1", 1),
                ("charge0", -1),
                ("charge1", -1),
            )
        )
    response = page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    )
    assert response.ok
    return body


def connect(page):
    """Authenticate once in page memory. No token is placed in persistent browser storage."""
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#installation-shell")).to_be_visible()
    if page.locator("#installation-wizard-actions").is_visible():
        expect(page.locator("#progress")).to_have_text("Stage 1 of 5")
        page.get_by_role("button", name="Continue", exact=True).click()


def installation_area(page, area, edit=False):
    """Select a maintenance area and explicitly unlock it when the scenario changes settings."""
    page.get_by_role("combobox", name="Installation area", exact=True).select_option(str(area))
    if edit:
        page.get_by_role("button", name="Edit installation", exact=True).click()


def show_installation_sources(page):
    """Open live source and model diagnostics during either first setup or maintenance."""
    expect(page.locator("#installation-shell")).to_be_visible()
    if page.locator("#installation-steps").is_visible():
        page.get_by_role("button", name="Home Assistant", exact=True).click()
        page.get_by_role("button", name="Price sources", exact=True).click()
    else:
        installation_area(page, 5)


def edit_strategy(page):
    """Open the existing strategy editor even while initial Home Assistant discovery is pending."""
    expect(page.locator("#installation-shell")).to_be_visible()
    if not page.locator("#strategy-settings").is_visible():
        if page.locator("#installation-strategy-shortcut").is_visible():
            page.get_by_role("button", name="Planning strategy", exact=True).click()
        else:
            installation_area(page, 4)
    page.get_by_role("button", name="Edit installation", exact=True).click()


def grid(page):
    """Use detected power/time zone and Energy balance. Ask for physical limits and annual scale only without history."""
    page.get_by_label("Maximum grid import (kW)", exact=True).fill("5")
    page.get_by_label("Maximum grid export (kW)", exact=True).fill("5")
    annual = page.get_by_label("Annual whole-home consumption (kWh)", exact=True)
    if annual.is_visible() and not annual.input_value():
        annual.fill("3500")
    page.get_by_role("button", name="Continue", exact=True).click()


def read_settings(page, url):
    """Inspect actual app-owned settings through its authenticated read endpoint."""
    response = page.request.get(url + "/v1/setup", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.ok
    return response.json()


def edit_price_rule(page):
    """Enter explicit edit mode when a saved contract is currently read-only."""
    button = page.get_by_role("button", name="Edit price rule", exact=True)
    expect(page.locator("#contract-editor")).to_be_visible()
    if button.is_visible():
        expect(button).to_be_enabled()
        button.click()


def add_price_rule(page):
    """Save the current valid rule before adding another through the read-only toolbar."""
    save = page.get_by_role("button", name="Save contract", exact=True)
    if save.is_visible():
        expect(save).to_be_enabled()
        save.click()
        expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    page.get_by_role("button", name="Add price rule", exact=True).click()


def assert_floating_save_bar(page):
    """Exactly one save bar and its readable status remain inside the viewport at either end of the page."""
    if page.locator("#installation-wizard-actions").is_visible():
        bar = page.locator("#installation-save-bar")
        bar.scroll_into_view_if_needed()
        expect(bar).to_be_visible()
        assert bar.get_by_role("status").inner_text()
        return
    if page.locator(".settings-toolbar").count():
        toolbar = page.locator(".settings-toolbar")
        expect(toolbar).to_be_visible()
        assert toolbar.evaluate("element => getComputedStyle(element).position") == "sticky"
        # Establish the natural toolbar position once, then test scrolling without corrective scrolling.
        page.evaluate("window.scrollTo(0, 0)")
        natural_top = toolbar.bounding_box()["y"]
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        bounds = toolbar.bounding_box()
        assert bounds is not None and bounds["y"] >= 0
        assert bounds["y"] + bounds["height"] <= page.viewport_size["height"] + 1
        assert toolbar.get_by_role("status").inner_text()
        page.evaluate("window.scrollTo(0, 0)")
        assert abs(toolbar.bounding_box()["y"] - natural_top) <= 1
        return
    bar = page.locator(".save-bar:visible")
    expect(bar).to_have_count(1)
    assert bar.evaluate("element => getComputedStyle(element).position") == "fixed"
    assert (
        bar.evaluate("element => getComputedStyle(element).backgroundColor") == "rgb(245, 251, 255)"
    )
    assert "Outfit" in bar.evaluate("element => getComputedStyle(element).fontFamily")
    for edge in ("document.body.scrollHeight", "0"):
        page.evaluate(f"window.scrollTo(0, {edge})")
        bounds = bar.bounding_box()
        assert bounds is not None
        assert bounds["x"] >= 0 and bounds["y"] >= 0
        assert bounds["x"] + bounds["width"] <= page.viewport_size["width"] + 1
        assert bounds["y"] + bounds["height"] <= page.viewport_size["height"] + 1
        assert bar.get_by_role("status").inner_text()
        expect(bar.get_by_role("button", name="Discard changes", exact=True)).to_be_visible()


def finish(page):
    """Advance the battery screen through server preview and commit the displayed installation."""
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_role("heading", name="Review your installation", exact=True)).to_be_visible()
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Installation saved.")


def physical_plan():
    """Specify independent terminal balances with separate converter and internal storage losses.

    Fixture-assumed internal charge efficiency is 0.95 and discharge efficiency 0.9,
    distinct from DC converter efficiency 0.8 and inverter efficiency 0.9.
    """
    return {
        "input_snapshot": {
            "problem": {
                "intervals": [
                    {
                        "start": "2026-10-23T00:00:00Z",
                        "duration_h": 0.25,
                        "load_kwh": 1,
                        "pv_kwh": 8,
                    },
                    {"start": "2026-10-23T00:15:00Z", "duration_h": 1, "load_kwh": 2, "pv_kwh": 0},
                    {"start": "2026-10-23T01:15:00Z", "duration_h": 1, "load_kwh": 1, "pv_kwh": 0},
                ],
                "inverters": [{"inverter_id": "hybrid", "efficiency": 0.9}],
                "pv_sources": [{"source_id": "roof", "inverter_id": "hybrid"}],
                "batteries": [
                    {
                        "device_id": "battery",
                        "inverter_id": "hybrid",
                        "dc_efficiency": 0.8,
                        "battery": {"capacity_kwh": 10, "initial_kwh": 5},
                    }
                ],
            }
        },
        "expected_flows": [
            {
                "grid_import": 0,
                "grid_export": 0.8,
                "solar": [{"source_id": "roof", "used_kwh": 4}],
                "inverters": [{"inverter_id": "hybrid", "import_kwh": 0, "export_kwh": 1.8}],
                "batteries": [
                    {"device_id": "battery", "charge_kwh": 1.6, "discharge_kwh": 0, "end_kwh": 6.52}
                ],
            },
            {
                "grid_import": 0.56,
                "grid_export": 0,
                "solar": [{"source_id": "roof", "used_kwh": 0}],
                "inverters": [{"inverter_id": "hybrid", "import_kwh": 0, "export_kwh": 1.44}],
                "batteries": [
                    {
                        "device_id": "battery",
                        "charge_kwh": 0,
                        "discharge_kwh": 2,
                        "end_kwh": 6.52 - 2 / 0.9,
                    }
                ],
            },
            {
                "grid_import": 3,
                "grid_export": 0,
                "solar": [{"source_id": "roof", "used_kwh": 0}],
                "inverters": [{"inverter_id": "hybrid", "import_kwh": 2, "export_kwh": 0}],
                "batteries": [
                    {
                        "device_id": "battery",
                        "charge_kwh": 1.44,
                        "discharge_kwh": 0,
                        "end_kwh": 6.52 - 2 / 0.9 + 1.44 * 0.95,
                    }
                ],
            },
        ],
    }


def balances(page, url, plan):
    """Load the same locally served presentation module used by the authenticated overview."""
    page.goto(url + "/overview")
    return page.evaluate(
        "async plan => {const {energyBalances} = await import('./setup/balance.js'); return energyBalances(plan, new Map([...Object.entries(plan.comparison_names || {}), ['battery','Home battery']]));}",
        plan,
    )
