"""Render the real thermal settings modules on desktop and mobile with controlled HTTP responses."""

import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.control_layout import assert_field_actions_aligned

ASSETS = Path(__file__).resolve().parents[3] / "src/open_energy_planner/web"
STORE = "00000000-0000-4000-8000-000000000101"


def document():
    """Declare one on/off boiler and independently specified quarter-hour chart evidence."""
    start = datetime.now(UTC).replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % 15)
    configuration = {
        "id": STORE,
        "energy_entity": "sensor.boiler_energy",
        "temperature_entity": "sensor.boiler_temperature",
        "power_entity": "sensor.boiler_power",
        "volume_litres": 120,
        "minimum_c": 45,
        "maximum_c": 65,
        "ambient_c": 20,
        "loss_w_per_c": 1.2,
        "heater_w": 2000,
        "draw_profile_kwh": [0] * 7 + [1] + [0] * 16,
        "enabled": True,
    }
    return {
        "configuration": {"stores": [configuration]},
        "configuration_version": "a" * 64,
        "revision": "b" * 64,
        "timezone": "Europe/Amsterdam",
        "issue": None,
        "catalogue": {
            "entities": [
                {
                    "entity_id": "sensor.boiler_temperature",
                    "name": "Temperature",
                    "device_name": "Utility boiler",
                    "device_class": "temperature",
                    "unit": "°C",
                },
                {
                    "entity_id": "sensor.unsupported_kelvin_temperature",
                    "name": "Unsupported temperature",
                    "device_name": "Utility boiler",
                    "device_class": "temperature",
                    "unit": "K",
                },
                {
                    "entity_id": "sensor.boiler_power",
                    "name": "Heater",
                    "device_name": "Utility boiler",
                    "device_class": "power",
                    "unit": "W",
                },
                {
                    "entity_id": "water_heater.boiler",
                    "name": "Tank",
                    "device_name": "Utility boiler",
                    "device_class": "temperature",
                    "unit": "°C",
                },
            ]
        },
        "energy_consumers": [
            {"entity_id": "sensor.boiler_energy", "name": "Utility boiler", "upstream": None}
        ],
        "devices": [
            {
                **configuration,
                "name": "Utility boiler",
                "actual": {"temperature_c": 49.25, "power_w": 0, "observed_at": start.isoformat()},
                "issue": None,
                "unmet_draw_kwh": 0.35,
                "planned": [
                    {
                        "start": (start + timedelta(minutes=15 * index)).isoformat(),
                        "end": (start + timedelta(minutes=15 * (index + 1))).isoformat(),
                        "power_w": power,
                        "end_c": temperature,
                        "shortfall_kwh": 0,
                    }
                    for index, (power, temperature) in enumerate(
                        ((2000, 52.5), (0, 52.1), (2000, 55.2))
                    )
                ],
            }
        ],
    }


@pytest.fixture(
    params=[
        {"width": 1440, "height": 1000},
        {"width": 768, "height": 1024},
        {"width": 390, "height": 844},
    ]
)
def thermal_page(browser, request):
    """Serve actual HTML/CSS/JS and Chart.js while exposing controlled versioned API writes."""
    context = browser.new_context(
        viewport=request.param, timezone_id="America/New_York", locale="en-US"
    )
    page = context.new_page()
    state = {"document": document(), "posts": [], "intents": [], "reject": False}
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve(route):
        """Return deterministic API evidence and otherwise load the repository's real web assets."""
        path = urlsplit(route.request.url).path
        if path == "/setup/session":
            route.fulfill(json={"ingress": True, "timezone": "Europe/Amsterdam"})
        elif path == "/v1/thermal":
            route.fulfill(json=state["document"])
        elif path == "/v1/thermal/configuration":
            body = route.request.post_data_json
            state["posts"].append(body)
            if (
                state["reject"]
                or body["expected_version"] != state["document"]["configuration_version"]
            ):
                route.fulfill(status=400, body="Thermal settings changed. Refresh before saving")
                return
            state["document"]["configuration"] = deepcopy(body["configuration"])
            state["document"]["configuration_version"] = "c" * 64
            route.fulfill(json=state["document"])
        elif path == "/v1/thermal/intent":
            body = route.request.post_data_json
            state["intents"].append(body)
            assert set(body) == {"device_id", "field", "value"}
            assert body["device_id"] in {row["id"] for row in state["document"]["devices"]}
            assert body["field"] in ("minimum_c", "enabled")
            if state["reject"]:
                route.fulfill(status=400, body="Thermal request rejected")
                return
            for rows in (
                state["document"]["configuration"]["stores"],
                state["document"]["devices"],
            ):
                row = next(row for row in rows if row["id"] == body["device_id"])
                row[body["field"]] = body["value"]
            route.fulfill(json=state["document"])
        elif path in ("/thermal", "/appliance-settings/thermal"):
            filename = "thermal.html" if path == "/thermal" else "appliance-settings/thermal.html"
            html = (ASSETS / "pages" / filename).read_text().replace("__OEP_BASE__", "/")
            html = html.replace(
                "<!-- OEP_NAVIGATION -->",
                '<aside class="sidebar"><nav><a href="/thermal">Thermal storage</a></nav></aside>',
            )
            html = html.replace(
                "<!-- OEP_APPLIANCE_SETTINGS -->",
                (ASSETS / "partials/appliance-settings-navigation.html").read_text(),
            )
            route.fulfill(body=html, content_type="text/html")
        elif path.startswith("/setup/"):
            asset = ASSETS / "public" / path.removeprefix("/setup/")
            content_type = (
                "text/javascript"
                if asset.suffix == ".js"
                else "text/css"
                if asset.suffix == ".css"
                else "image/svg+xml"
                if asset.suffix == ".svg"
                else "font/ttf"
            )
            route.fulfill(path=asset, content_type=content_type)
        else:
            route.abort()

    page.route("http://thermal.test/**", serve)
    page.goto("http://thermal.test/appliance-settings/thermal")
    expect(page.locator("#thermal-title")).to_have_text("Utility boiler")
    try:
        yield page, state
        assert not errors
    finally:
        context.close()


@pytest.fixture
def thermal_monitor_page(thermal_page):
    """Use the operational route with the same independent measured and planned evidence."""
    page, state = thermal_page
    page.goto("http://thermal.test/thermal")
    expect(page.locator("#thermal-title")).to_have_text("Utility boiler")
    return page, state


def test_comfort_actions_align_with_field_on_desktop_tablet_and_mobile(thermal_monitor_page):
    """Comfort actions share a level row and wrap below the field on smaller screens."""
    page, _state = thermal_monitor_page
    page.evaluate("document.fonts.ready")
    assert_field_actions_aligned(page.locator("#comfort-request"), expected_buttons=2)
    page.screenshot(path=f"results/ci/thermal-controls-{page.viewport_size['width']}.png")


def test_monitor_panels_keep_one_gap_when_the_plan_is_hidden(thermal_monitor_page):
    """Populated panels and missing-plan panels share spacing without a phantom empty row."""
    page, state = thermal_monitor_page
    expected_gap = 16 if page.viewport_size["width"] <= 640 else 24
    for missing_plan in (False, True):
        if missing_plan:
            for device in state["document"]["devices"]:
                device["planned"] = []
            page.get_by_role("button", name="Refresh evidence", exact=True).click()
            expect(page.locator("#thermal-chart-card")).to_be_hidden()
        panels = page.locator("#thermal-monitor > .chart-card:visible")
        expect(panels).to_have_count(3 if missing_plan else 4)
        bounds = [panel.bounding_box() for panel in panels.all()]
        for previous, following in pairwise(bounds):
            assert following["y"] - previous["y"] - previous["height"] == pytest.approx(
                expected_gap
            )
        page.screenshot(
            path=f"results/ci/panel-spacing-thermal-{page.viewport_size['width']}-{missing_plan}.png",
            full_page=True,
        )


def test_thermal_edit_cancel_save_and_readonly_refresh_preserve_drafts(thermal_page):
    """Evidence refresh cannot replace a 2.5-kW draft. Cancel restores 2 kW and Save persists watts once."""
    page, state = thermal_page
    power = page.locator('[data-field="heater_w"]')
    expect(power).to_have_value("2")
    expect(power).to_be_disabled()
    page.locator("#edit-thermal").click()
    expect(page.locator("#cancel-thermal")).to_be_enabled()
    power.fill("2.5")
    page.locator("#refresh-thermal-evidence").click()
    expect(power).to_have_value("2.5")
    expect(page.locator("#thermal-save-state")).to_contain_text("Unsaved changes")
    page.get_by_role("navigation", name="Appliance settings", exact=True).get_by_role(
        "link", name="EV charging", exact=True
    ).click()
    expect(page).to_have_url("http://thermal.test/appliance-settings/thermal")
    expect(power).to_have_value("2.5")
    expect(page.locator("#thermal-save-state")).to_contain_text("Save or discard")
    page.locator("#cancel-thermal").click()
    expect(power).to_have_value("2")
    expect(power).to_be_disabled()
    page.locator("#edit-thermal").click()
    power.fill("2.5")
    page.locator("#save-thermal").click()
    expect(page.locator("#thermal-save-state")).to_have_text("All changes saved.")
    assert len(state["posts"]) == 1
    page.evaluate("scrollTo({top: 0, behavior: 'instant'})")
    page.screenshot(
        path=f"results/ci/boiler-settings-{page.viewport_size['width']}.png", full_page=True
    )
    assert state["posts"][0]["configuration"]["stores"][0]["heater_w"] == 2500
    expect(power).to_have_value("2.5")
    expect(power).to_be_disabled()


def test_thermal_conflict_and_failed_save_retain_all_input(thermal_page):
    """A rejected write retains edits, and a subsequent version conflict suppresses another save."""
    page, state = thermal_page
    page.locator("#edit-thermal").click()
    power = page.locator('[data-field="heater_w"]')
    power.fill("3")
    state["reject"] = True
    page.locator("#save-thermal").click()
    expect(page.locator("#thermal-save-state")).to_contain_text("Refresh before saving")
    expect(power).to_have_value("3")
    expect(power).to_be_enabled()
    state["document"]["configuration_version"] = "d" * 64
    page.locator("#refresh-thermal-evidence").click()
    expect(page.locator("#thermal-save-state")).to_contain_text("changed elsewhere")
    expect(page.locator("#save-thermal")).to_be_disabled()
    expect(power).to_have_value("3")


def test_selected_boiler_remove_cancel_and_save_preserve_other_identity_and_physics(thermal_page):
    """Removal is staged. Cancel restores the selected store, and Save preserves its neighbour exactly."""
    page, state = thermal_page
    first = deepcopy(state["document"]["configuration"]["stores"][0])
    second = {**deepcopy(first), "id": "00000000-0000-4000-8000-000000000102"}
    second["energy_entity"] = "sensor.second_boiler_energy"
    state["document"]["configuration"]["stores"].append(second)
    state["document"]["energy_consumers"].append(
        {"entity_id": second["energy_entity"], "name": "Second boiler", "upstream": None}
    )
    page.reload()
    selector = page.locator("#thermal-store-select")
    expect(selector.locator("option")).to_have_count(2)
    selector.select_option(second["id"])
    expect(page.locator("#thermal-summary")).to_contain_text(second["energy_entity"])
    expect(page.locator("#thermal-editor")).to_be_hidden()

    def remove_selected():
        """Use the visible More action rather than bypassing the saved-state toolbar."""
        page.locator("#thermal-more summary").click()
        page.locator("#remove-thermal").click()
        page.get_by_role("alertdialog", name="Remove electric boiler?", exact=True).get_by_role(
            "button", name="Remove boiler", exact=True
        ).click()

    remove_selected()
    expect(selector).to_be_disabled()
    assert state["posts"] == []
    page.locator("#cancel-thermal").click()
    expect(selector).to_have_value(second["id"])
    expect(selector.locator("option")).to_have_count(2)
    assert state["posts"] == []
    remove_selected()
    page.locator("#save-thermal").click()
    expect(page.locator("#thermal-save-state")).to_have_text("All changes saved.")
    assert state["posts"][-1]["configuration"]["stores"] == [first]
    expect(selector).to_have_value(first["id"])
    remove_selected()
    expect(page.locator("#thermal-empty")).to_be_visible()
    page.locator("#cancel-thermal").click()
    expect(selector).to_have_value(first["id"])
    remove_selected()
    page.locator("#save-thermal").click()
    expect(page.locator("#thermal-save-state")).to_have_text("All changes saved.")
    assert state["posts"][-1]["configuration"]["stores"] == []
    expect(page.locator("#add-thermal")).to_be_enabled()


def test_heater_kw_input_saves_whole_watts_without_accepting_fractional_watts(thermal_page):
    """2.002 kW saves exactly 2002 W, while 2.0021 kW fails native step validation without a write."""
    page, state = thermal_page
    page.locator("#edit-thermal").click()
    power = page.locator('[data-field="heater_w"]')
    expect(power).to_have_attribute("step", "0.001")
    power.fill("2.0021")
    assert power.evaluate("input => input.validity.stepMismatch")
    expect(page.locator("#save-thermal")).to_be_disabled()
    assert state["posts"] == []
    power.fill("2.002")
    assert power.evaluate("input => input.validity.valid")
    page.locator("#save-thermal").click()
    expect(page.locator("#thermal-save-state")).to_have_text("All changes saved.")
    assert state["posts"][0]["configuration"]["stores"][0]["heater_w"] == 2002
    expect(power).to_have_value("2.002")


@pytest.mark.parametrize(
    "unmet_draw,display", [(0.35, "0.350"), (0, "0.000"), (None, "Unavailable")]
)
def test_unserved_hot_water_is_distinct_from_comfort_and_ending_stock(
    thermal_monitor_page, unmet_draw, display
):
    """Physical unserved demand has its own value. Missing evidence does not become zero or a comfort deficit."""
    page, state = thermal_monitor_page
    state["document"]["devices"][0]["unmet_draw_kwh"] = unmet_draw
    state["document"]["devices"][0]["ending_stock_shortfall_kwh"] = 0.125
    page.locator("#refresh-thermal-evidence").click()
    evidence = page.locator("#thermal-evidence")
    expect(evidence).to_contain_text(f"Unserved hot-water heat demand: {display} thermal kWh")
    expect(evidence).to_contain_text("Largest predicted comfort deficit: 0.000 thermal kWh")
    expect(evidence).to_contain_text("Predicted ending stock deficit: 0.125 thermal kWh")
    expect(page.locator("#thermal-plan-summary")).to_have_text(
        "Utility boiler: 3 planned intervals: Comfort target: 45.0 °C: Maximum planning temperature: 65.0 °C"
    )


def test_new_boiler_has_no_invented_physics_or_enabled_permission(thermal_page):
    """Adding a store creates a disabled draft with empty physics and an explicitly assumed draw profile."""
    page, state = thermal_page
    page.locator("#add-thermal").click()
    expect(page.locator('[data-field="enabled"]')).not_to_be_checked()
    for field in (
        "minimum_c",
        "maximum_c",
        "volume_litres",
        "heater_w",
        "loss_w_per_c",
        "ambient_c",
    ):
        expect(page.locator(f'[data-field="{field}"]')).to_have_value("")
    expect(page.locator("#save-thermal")).to_be_disabled()
    page.locator("#thermal-demand-profile summary").click()
    expect(page.locator("#thermal-demand-profile")).to_contain_text("assumptions, not measured")
    expect(page.locator('[data-hour="7"]')).to_have_value("0")
    expect(
        page.locator('[data-field="temperature_entity"] option[value="water_heater.boiler"]')
    ).to_have_count(1)
    expect(
        page.locator(
            '[data-field="temperature_entity"] option[value="sensor.unsupported_kelvin_temperature"]'
        )
    ).to_have_count(0)
    page.locator("#cancel-thermal").click()
    expect(page.locator("#thermal-store-select option")).to_have_count(1)
    assert state["posts"] == []


def test_thermal_chart_and_measured_request_separation_are_responsive(thermal_monitor_page):
    """Interactive power bars retain 15-minute times and a separate one-decimal temperature axis."""
    page, _state = thermal_monitor_page
    expect(page.locator("#thermal-evidence")).to_contain_text("Measured temperature: 49.3 °C")
    expect(page.locator("#thermal-evidence")).to_contain_text("Measured heater power: 0.00 kW")
    expect(page.locator("#thermal-evidence")).to_contain_text("Planned heating request: On")
    canvas = page.locator("#thermal-plan-chart")
    expect(canvas).to_be_visible()
    evidence = canvas.evaluate(
        "canvas => { const chart = Chart.getChart(canvas); return {bars: chart.data.datasets[0].data.map(point => point.y), temperatures: chart.data.datasets[1].data.map(point => point.y), ticks: chart.scales.x.ticks.map(tick => tick.value), minimum: chart.scales.y.min, width: canvas.getBoundingClientRect().width}; }"
    )
    assert evidence["bars"] == [2, 0, 2]
    assert evidence["temperatures"] == [52.5, 52.1, 55.2]
    assert evidence["minimum"] == 0
    assert all(tick % 900000 == 0 for tick in evidence["ticks"])
    assert evidence["width"] <= page.viewport_size["width"]
    page.evaluate("scrollTo({top: 0, behavior: 'instant'})")
    page.screenshot(
        path=f"results/ci/boiler-controls-{page.viewport_size['width']}.png", full_page=True
    )
    canvas.evaluate("canvas => { const chart = Chart.getChart(canvas); chart.hide(0); }")
    assert canvas.evaluate("canvas => !Chart.getChart(canvas).isDatasetVisible(0)")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_thermal_axes_and_tooltips_omit_timezone_names_during_wintertime(thermal_monitor_page):
    """Repeated 02:30 times retain separate points without timezone names in axes or tooltips on desktop, tablet and mobile."""
    page, state = thermal_monitor_page
    state["document"]["devices"][0]["planned"] = [
        {
            "start": f"2026-10-25T0{hour}:30:00Z",
            "end": f"2026-10-25T0{hour}:45:00Z",
            "power_w": 2000,
            "end_c": 52.5,
            "shortfall_kwh": 0,
        }
        for hour in (0, 1)
    ]
    page.locator("#refresh-thermal-evidence").click()
    canvas = page.locator("#thermal-plan-chart")
    expect(page.locator("#thermal-plan-summary")).to_contain_text("2 planned intervals")
    axis_labels = canvas.evaluate(
        "canvas=>Chart.getChart(canvas).scales.x.ticks.map(tick=>tick.label)"
    )
    assert axis_labels
    assert all(re.fullmatch(r"\d{2}:\d{2}", label) for label in axis_labels)
    for index in range(2):
        point = canvas.evaluate(
            "(canvas,index)=>Chart.getChart(canvas).getDatasetMeta(0).data[index].getCenterPoint()",
            index,
        )
        canvas.hover(position={"x": point["x"], "y": point["y"]})
        assert canvas.evaluate("canvas=>Chart.getChart(canvas).tooltip.title") == [
            "10/25/2026, 2:30 AM to 02:45 AM"
        ]


def test_category_navigation_retains_unsaved_boiler_configuration(thermal_page):
    """Opening another type is refused until a boiler draft is saved or explicitly discarded."""
    page, state = thermal_page
    page.locator("#edit-thermal").click()
    page.locator('[data-field="heater_w"]').fill("2.5")
    page.get_by_role("navigation", name="Appliance settings", exact=True).get_by_role(
        "link", name="EV charging", exact=True
    ).click()
    expect(page).to_have_url("http://thermal.test/appliance-settings/thermal")
    expect(page.locator('[data-field="heater_w"]')).to_have_value("2.5")
    expect(page.locator("#thermal-save-state")).to_contain_text("Save or discard")
    assert state["posts"] == []


def test_energy_boiler_intents_do_not_replace_configuration(thermal_monitor_page):
    """Daily comfort and pause actions use individual intents while all model bindings remain intact."""
    page, state = thermal_monitor_page
    original = deepcopy(state["document"]["configuration"])
    expect(page.get_by_role("button", name="Add electric boiler", exact=True)).to_have_count(0)
    expect(page.locator('[data-field="volume_litres"]')).to_have_count(0)
    expect(
        page.get_by_role("link", name="Configure electric boilers in Appliances settings")
    ).to_have_attribute("href", "/appliance-settings/thermal")
    target = page.get_by_label("Comfort target (°C)", exact=True)
    target.fill("47.125")
    page.locator("#refresh-thermal-evidence").click()
    target.focus()
    expect(target).to_have_value("47.125")
    page.get_by_role("button", name="Pause thermal planning", exact=True).click()
    expect(page.get_by_role("button", name="Enable thermal planning", exact=True)).to_be_visible()
    target.focus()
    expect(target).to_have_value("47.125")
    page.get_by_role("button", name="Apply comfort target", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Request saved")
    assert state["intents"] == [
        {"device_id": STORE, "field": "enabled", "value": False},
        {"device_id": STORE, "field": "minimum_c", "value": 47.125},
    ]
    original["stores"][0].update(enabled=False, minimum_c=47.125)
    assert state["document"]["configuration"] == original
    assert state["posts"] == []


def test_rejected_operational_boiler_request_retains_input(thermal_monitor_page):
    """A rejected daily request shows its error and leaves actual configuration unchanged."""
    page, state = thermal_monitor_page
    state["reject"] = True
    page.get_by_label("Comfort target (°C)", exact=True).fill("49.125")
    page.get_by_role("button", name="Apply comfort target", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Thermal request rejected")
    target = page.get_by_label("Comfort target (°C)", exact=True)
    target.focus()
    expect(target).to_have_value("49.125")
    assert state["document"]["configuration"]["stores"][0]["minimum_c"] == 45
    assert state["posts"] == []


def test_late_thermal_evidence_cannot_undo_committed_intent(thermal_monitor_page):
    """A GET started before a successful pause must not restore the obsolete enabled state."""
    page, state = thermal_monitor_page
    delayed = []
    obsolete = deepcopy(state["document"])

    def hold(route):
        """Hold exactly one pre-intent response while later reads use current evidence."""
        if not delayed:
            delayed.append(route)
        else:
            route.fallback()

    page.route("http://thermal.test/v1/thermal", hold)
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(page.get_by_role("button", name="Pause thermal planning", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Pause thermal planning", exact=True).click()
    expect(page.get_by_role("button", name="Enable thermal planning", exact=True)).to_be_visible()
    delayed[0].fulfill(json=obsolete)
    # Drain the obsolete fetch and its promise continuations before reading the DOM.
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    expect(page.get_by_role("button", name="Enable thermal planning", exact=True)).to_be_visible()
    assert state["document"]["devices"][0]["enabled"] is False


@pytest.mark.parametrize("failed_refresh", [False, True])
def test_selecting_boiler_aligns_controls_evidence_and_intents_before_refresh(
    thermal_monitor_page, failed_refresh
):
    """Two distinct boiler snapshots must switch synchronously even when refreshing is delayed or fails."""
    page, state = thermal_monitor_page
    second_id = "00000000-0000-4000-8000-000000000102"
    second = deepcopy(state["document"]["devices"][0])
    second.update(
        id=second_id,
        name="Loft boiler",
        energy_entity="sensor.loft_boiler_energy",
        temperature_entity="sensor.loft_boiler_temperature",
        power_entity="sensor.loft_boiler_power",
        heater_w=3000,
        minimum_c=52.125,
        enabled=False,
    )
    second["actual"].update(temperature_c=61.75, power_w=3000)
    for interval in second["planned"]:
        interval.update(power_w=3000, end_c=63.5)
    configuration = deepcopy(state["document"]["configuration"]["stores"][0])
    configuration.update(
        id=second_id,
        energy_entity="sensor.loft_boiler_energy",
        temperature_entity="sensor.loft_boiler_temperature",
        power_entity="sensor.loft_boiler_power",
        heater_w=3000,
        minimum_c=52.125,
        enabled=False,
    )
    state["document"]["energy_consumers"].append(
        {"entity_id": "sensor.loft_boiler_energy", "name": "Loft boiler", "upstream": None}
    )
    for entity_id, device_class, unit in (
        ("sensor.loft_boiler_temperature", "temperature", "°C"),
        ("sensor.loft_boiler_power", "power", "W"),
    ):
        state["document"]["catalogue"]["entities"].append(
            {
                "entity_id": entity_id,
                "name": device_class.capitalize(),
                "device_name": "Loft boiler",
                "device_class": device_class,
                "unit": unit,
            }
        )
    state["document"]["devices"].append(second)
    state["document"]["configuration"]["stores"].append(configuration)
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(page.locator("#thermal-store-select option")).to_have_count(2)
    requests = []
    delayed_document = deepcopy(state["document"])

    def refresh(route):
        """Delay or reject only the selection read. Subsequent intent evidence uses current state."""
        if not requests:
            requests.append(route)
            if failed_refresh:
                route.fulfill(status=503, body="Thermal refresh unavailable")
        else:
            route.fallback()

    page.route("http://thermal.test/v1/thermal", refresh)
    page.get_by_label("Comfort target (°C)", exact=True).fill("49.625")
    page.locator("#thermal-store-select").focus()
    page.locator("#thermal-store-select").select_option(second_id)
    expect(page.locator("#thermal-title")).to_have_text("Loft boiler")
    expect(page.get_by_label("Comfort target (°C)", exact=True)).to_have_value("52.1")
    expect(page.get_by_role("button", name="Enable thermal planning", exact=True)).to_be_enabled()
    expect(page.locator("#thermal-evidence")).to_contain_text("Measured temperature: 61.8 °C")
    expect(page.locator("#thermal-evidence")).to_contain_text("Measured heater power: 3.00 kW")
    assert page.locator("#thermal-plan-chart").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets[0].data.map(point => point.y)"
    ) == [3, 3, 3]
    if failed_refresh:
        expect(page.locator("#status")).to_have_text("Thermal refresh unavailable")
    page.get_by_role("button", name="Apply comfort target", exact=True).click()
    expect(page.get_by_role("button", name="Enable thermal planning", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Enable thermal planning", exact=True).click()
    expect(page.get_by_role("button", name="Pause thermal planning", exact=True)).to_be_enabled()
    assert state["intents"] == [
        {"device_id": second_id, "field": "minimum_c", "value": 52.125},
        {"device_id": second_id, "field": "enabled", "value": True},
    ]
    assert state["document"]["configuration"]["stores"][0]["minimum_c"] == 45
    assert state["document"]["devices"][0]["enabled"] is True
    if not failed_refresh:
        with page.expect_response(
            lambda response: response.url == "http://thermal.test/v1/thermal"
        ):
            requests[0].fulfill(json=delayed_document)
        page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        expect(
            page.get_by_role("button", name="Pause thermal planning", exact=True)
        ).to_be_enabled()
    expect(page.locator("#thermal-title")).to_have_text("Loft boiler")


def test_comfort_reset_restores_cached_target_when_refresh_fails(thermal_monitor_page):
    """Reset restores the saved comfort value immediately. A failed read cannot leave a hidden draft ready to submit."""
    page, state = thermal_monitor_page
    target = page.get_by_label("Comfort target (°C)", exact=True)
    target.fill("49.125")
    page.route(
        "http://thermal.test/v1/thermal",
        lambda route: route.fulfill(status=503, body="Thermal refresh unavailable"),
    )
    page.get_by_role("button", name="Use current target", exact=True).click()
    expect(target).to_have_value("45.0")
    expect(page.locator("#status")).to_have_text("Thermal refresh unavailable")
    page.unroute("http://thermal.test/v1/thermal")
    page.get_by_role("button", name="Apply comfort target", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Request saved")
    assert state["intents"] == [{"device_id": STORE, "field": "minimum_c", "value": 45}]
