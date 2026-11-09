"""Exercise complete chooser publication without replacing drafts, focus or saved identities."""

from copy import deepcopy
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    TOKEN,
    add_thermal_appliance,
    connect,
    discover,
    discover_boiler_sources,
    read_settings,
    wait_for_product_reads,
)
from tests.frontend.browser.test_appliance_settings import add_ev_appliance


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 900}, {"width": 390, "height": 844}], indirect=True
)
def test_on_demand_vehicle_sources_retain_inputs_focus_and_cancel(app_page):
    """Late candidate publication adds an unconfigured car while retaining a draft and Cancel semantics."""
    page, url = app_page
    body = discover(page, url)
    candidates = deepcopy(body)
    candidates["entities"].append(
        {
            "entity_id": "sensor.synthetic_zeekr_soc",
            "name": "Battery level",
            "device_name": "Synthetic Zeekr",
            "unit": "%",
            "device_id": "synthetic-zeekr",
            "device_class": "battery",
            "state_class": "measurement",
            "integration_id": "synthetic-zeekr-entry",
            "integration_name": "Synthetic Zeekr",
        }
    )
    headers = {"Authorization": "Bearer " + TOKEN}
    page.goto(url + "/appliance-settings/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/source-choices/ev")
        )
    ) as pending:
        add_ev_appliance(page, "vehicle")
    nonce = pending.value.json()["id"]
    capacity = page.get_by_label("Usable battery capacity (kWh)", exact=True)
    capacity.fill("74.5")
    page.get_by_label("Maximum vehicle AC charging power (kW)", exact=True).fill("11")
    capacity.focus()
    expect(page.locator("#status")).to_contain_text("Your edits are retained")
    result = page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "ev", "id": nonce, "page": candidates},
    )
    assert result.ok and result.json()["accepted"]
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(capacity).to_have_value("74.5")
    expect(capacity).to_be_focused()
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option(
        "synthetic-zeekr-entry"
    )
    page.get_by_label("Vehicle state of charge", exact=True).select_option(
        "sensor.synthetic_zeekr_soc"
    )
    expect(page.locator("#dirty")).to_contain_text("Unsaved changes")
    active = page.request.get(url + "/v1/setup", headers=headers).json()["catalogue"]["data"]
    assert all(row["entity_id"] != "sensor.synthetic_zeekr_soc" for row in active["entities"])
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/source-choices-ev-{page.viewport_size['width']}.png", full_page=True
    )
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    assert page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"] == []


def test_failed_choice_refresh_retains_existing_draft_and_explicit_retry(app_page):
    """A reported HA failure retains numeric edits and permits a fresh user-requested nonce."""
    page, url = app_page
    discover(page, url)
    headers = {"Authorization": "Bearer " + TOKEN}
    page.goto(url + "/appliance-settings/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/source-choices/ev")
        )
    ) as pending:
        add_ev_appliance(page, "vehicle")
    first = pending.value.json()["id"]
    capacity = page.get_by_label("Usable battery capacity (kWh)", exact=True)
    capacity.fill("62")
    assert page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "ev", "id": first, "failed": True},
    ).ok
    expect(page.locator("#status")).to_contain_text("could not be refreshed")
    expect(capacity).to_have_value("62")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/source-choices/ev")
        )
    ) as retry:
        page.get_by_role("button", name="Refresh Home Assistant sources", exact=True).click()
    assert retry.value.json()["id"] != first
    expect(capacity).to_have_value("62")


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 900}, {"width": 390, "height": 844}], indirect=True
)
def test_boiler_source_refresh_preserves_raw_inputs_and_missing_selection(app_page):
    """A late water-heater choice updates options while preserving an unsaved boiler and focus."""
    page, url = app_page
    body = discover_boiler_sources(page, url)
    candidates = deepcopy(body)
    candidates["entities"].append(
        {
            "entity_id": "water_heater.synthetic_tank",
            "name": "Synthetic hot-water tank",
            "unit": "°C",
            "device_id": "synthetic-tank",
            "device_class": "temperature",
            "state_class": "",
        }
    )
    headers = {"Authorization": "Bearer " + TOKEN}
    page.goto(url + "/appliance-settings/thermal")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/v1/ha/source-choices/thermal")
        )
    ) as pending:
        add_thermal_appliance(page)
    volume = page.get_by_label("Water volume (litres)", exact=False)
    volume.fill("180")
    volume.focus()
    nonce = pending.value.json()["id"]
    assert page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "thermal", "id": nonce, "page": candidates},
    ).ok
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(volume).to_have_value("180")
    expect(volume).to_be_focused()
    page.get_by_label("Measured water temperature", exact=False).select_option(
        "water_heater.synthetic_tank"
    )
    page.get_by_label("Electrical heating power (kW)", exact=False).fill("2")
    page.get_by_label("Minimum comfort temperature (°C)", exact=False).fill("45")
    page.get_by_label("Maximum planning temperature (°C)", exact=False).fill("65")
    page.get_by_label("Tank surroundings temperature (°C)", exact=False).fill("20")
    page.get_by_label("Heat-loss coefficient (W/°C)", exact=False).fill("1.2")
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/source-choices-thermal-{page.viewport_size['width']}.png", full_page=True
    )
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/v1/ha/source-choices/thermal")
        )
    ) as retry:
        page.get_by_role("button", name="Refresh Home Assistant sources", exact=True).click()
    assert page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "thermal", "id": retry.value.json()["id"], "page": body},
    ).ok
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(page.get_by_label("Measured water temperature", exact=False)).to_have_value(
        "water_heater.synthetic_tank"
    )
    expect(
        page.get_by_text("water_heater.synthetic_tank: Source unavailable", exact=True)
    ).to_have_count(1)
    expect(volume).to_have_value("180")


def test_installation_force_refresh_retains_unsaved_connection_limit(app_page):
    """A manual active-source refresh leaves an open installation draft and saved settings unchanged."""
    page, url = app_page
    body = discover(page, url)
    connect(page)
    maximum = page.get_by_label("Maximum grid import (kW)", exact=True)
    maximum.fill("7.25")
    before = read_settings(page, url)["installation"]
    page.get_by_role("button", name="Home Assistant", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/catalogue/refresh")
        )
    ):
        page.get_by_role("button", name="Refresh Home Assistant data", exact=True).click()
    expect(maximum).to_have_value("7.25")
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": "Bearer " + TOKEN}, data=body
    ).ok
    expect(page.locator("#installation-save-state")).to_contain_text("sources refreshed")
    expect(maximum).to_have_value("7.25")
    assert read_settings(page, url)["installation"] == before


@pytest.mark.parametrize("outcome", ["empty", "error", "late"])
def test_source_refresh_never_claims_unavailable_or_late_results_and_owns_errors(app_page, outcome):
    """Empty, private-error and deadline-expired responses retain drafts and allow independent retry."""
    page, url = app_page
    page.goto(url + "/setup")
    result = page.evaluate(
        """async outcome => {
            const {sourceRefresh} = await import('/setup/source-refresh.js');
            const button = document.createElement('button'), status = document.createElement('p');
            const input = document.createElement('input'); input.value = '7.25';
            document.body.append(button, status, input);
            const nativeTimeout = AbortSignal.timeout.bind(AbortSignal);
            const deadlines = [];
            AbortSignal.timeout = milliseconds => { deadlines.push(milliseconds); return nativeTimeout(10); };
            const applied = []; let attempt = 0, signal;
            const catalogue = {entities:[{entity_id:'sensor.synthetic',unit:'W'}]};
            const api = async (_path, _body, requestSignal) => {
                signal = requestSignal;
                if (++attempt > 1) return {catalogue};
                if (outcome === 'error') throw new Error('PRIVATE-SENTINEL-' + 'x'.repeat(10000));
                if (outcome === 'empty') return {catalogue:{entities:[]}};
                await new Promise(resolve => setTimeout(resolve, 30));
                return {catalogue};
            };
            const refresh = sourceRefresh(api, 'ev', button, status, value => { applied.push(value); });
            await refresh();
            const first = {message:status.textContent,applied:applied.length,disabled:button.disabled,
                value:input.value,aborted:signal?.aborted ?? false};
            await refresh();
            AbortSignal.timeout = nativeTimeout;
            return {first, final:status.textContent,attempt,deadlines,applied:applied.length};
        }""",
        outcome,
    )
    assert result["first"]["value"] == "7.25"
    assert not result["first"]["disabled"]
    assert "sources refreshed" not in result["first"]["message"]
    assert "PRIVATE-SENTINEL" not in result["first"]["message"]
    assert len(result["first"]["message"]) < 300
    assert result["deadlines"] == [65000, 65000]
    if outcome == "empty":
        assert "no sources" in result["first"]["message"]
        assert result["first"]["applied"] == 1
    elif outcome == "late":
        assert result["first"]["aborted"]
        assert result["first"]["applied"] == 0
    assert "sources refreshed" in result["final"]
    assert result["attempt"] == 2


def test_empty_accepted_sources_show_unavailability_with_retained_vehicle_draft(app_page):
    """A genuine empty HA reply preserves numeric inputs and exposes recovery, Save and Cancel."""
    page, url = app_page
    body = discover(page, url)
    empty = {
        "entities": [],
        "suggestions": {
            "terms": [],
            "soc": [],
            "solar": False,
            "solar_sources": [],
            "grid_power": None,
            "grid_power_entities": [],
            "energy_status": "missing",
            "batteries": [],
            "grid_export": None,
        },
        "timezone": body["timezone"],
        "history": [],
    }
    headers = {"Authorization": "Bearer " + TOKEN}
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=empty).ok
    page.goto(url + "/appliance-settings/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/source-choices/ev")
        )
    ) as pending:
        add_ev_appliance(page, "vehicle")
    capacity = page.get_by_label("Usable battery capacity (kWh)", exact=True)
    capacity.fill("64")
    page.get_by_label("Maximum vehicle AC charging power (kW)", exact=True).fill("11")
    assert page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "ev", "id": pending.value.json()["id"], "page": empty},
    ).ok
    expect(page.locator("#status")).to_contain_text("returned no sources")
    expect(capacity).to_have_value("64")
    expect(page.get_by_role("button", name="Cancel", exact=True)).to_be_visible()
    expect(page.locator("#save")).to_be_visible()
    expect(
        page.get_by_role("button", name="Refresh Home Assistant sources", exact=True)
    ).to_be_enabled()
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/source-choices-empty-ev-{page.viewport_size['width']}.png", full_page=True
    )
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")


@pytest.mark.parametrize(
    ("editor", "scenario"),
    [
        (editor, scenario)
        for editor in ("ev", "thermal", "installation")
        for scenario in ("initial", "status", "error")
    ]
    + [("installation", "apply")],
)
def test_actual_source_refresh_clients_forward_abort_signal_and_retain_drafts(
    app_page, editor, scenario
):
    """Every real source-refresh caller forwards cancellation and releases its button for explicit retry."""
    page, url = app_page
    discover_boiler_sources(page, url)
    if editor == "installation":
        connect(page)
        field = page.get_by_label("Maximum grid import (kW)", exact=True)
        field.fill("7.25")
        page.get_by_role("button", name="Home Assistant", exact=True).click()
        button = page.get_by_role("button", name="Refresh Home Assistant data", exact=True)
    else:
        page.goto(url + "/appliance-settings/" + editor)
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
        button = page.get_by_role("button", name="Refresh Home Assistant sources", exact=True)
    wait_for_product_reads(page)
    page.evaluate(
        """scenario => {
            const nativeFetch = window.fetch, nativeTimeout = AbortSignal.timeout.bind(AbortSignal);
            window.sourceReads = [];
            window.errorBodyReads = 0;
            window.sourceBudgets = [];
            const controllers = new WeakMap();
            AbortSignal.timeout = milliseconds => {
                if (milliseconds !== 65000) return nativeTimeout(milliseconds);
                window.sourceBudgets.push(milliseconds);
                const controller = new AbortController();
                controllers.set(controller.signal, controller);
                return controller.signal;
            };
            window.fetch = (path, options) => {
                const refresh = String(path).includes('/ha/');
                if (!refresh && !(scenario === 'apply' && String(path).endsWith('v1/setup'))) return nativeFetch(path, options);
                window.sourceReads.push({hasSignal:options.signal instanceof AbortSignal,aborted:false,signal:options.signal});
                const record = window.sourceReads.at(-1);
                if (scenario === 'apply' && refresh)
                    return Promise.resolve(new Response(JSON.stringify({pending:false}), {headers:{'Content-Type':'application/json'}}));
                if (scenario === 'status' && options.method === 'POST')
                    return Promise.resolve(new Response(JSON.stringify({pending:true}), {headers:{'Content-Type':'application/json'}}));
                if (scenario === 'error') {
                    const response = new Response('PRIVATE-HTTP-SENTINEL-' + 'x'.repeat(10000), {status:500});
                    const text = response.text.bind(response);
                    response.text = () => { ++window.errorBodyReads; return text(); };
                    return Promise.resolve(response);
                }
                if (options.signal?.aborted) {
                    record.aborted = true;
                    return Promise.reject(new DOMException('PRIVATE-NETWORK-SENTINEL', 'AbortError'));
                }
                return new Promise((resolve, reject) => {
                    options.signal?.addEventListener('abort', () => {
                        record.aborted = true;
                        reject(new DOMException('PRIVATE-NETWORK-SENTINEL', 'AbortError'));
                    }, {once:true});
                    // Cancel only after the intended hanging read has installed its listener.
                    queueMicrotask(() => controllers.get(options.signal).abort());
                });
            };
        }""",
        scenario,
    )
    if editor == "ev":
        add_ev_appliance(page, "vehicle")
        field = page.get_by_label("Capacity (kWh)", exact=False)
        field.fill("64")
    elif editor == "thermal":
        add_thermal_appliance(page)
        field = page.get_by_label("Water volume (litres)", exact=False)
        field.fill("180")
    else:
        button.click()
    expected = 2 if scenario in {"status", "apply"} else 1
    page.wait_for_function("count => window.sourceReads.length === count", arg=expected)
    assert page.evaluate("window.sourceBudgets") == [65000]
    assert page.evaluate("window.sourceReads[0].hasSignal")
    if scenario != "error":
        page.wait_for_function("() => window.sourceReads.at(-1).aborted")
    if scenario in {"status", "apply"}:
        assert page.evaluate("window.sourceReads[0].signal === window.sourceReads[1].signal")
    expect(button).to_be_enabled()
    status = page.locator("#installation-save-state" if editor == "installation" else "#status")
    expect(status).to_contain_text("could not be refreshed")
    expect(status).not_to_contain_text("PRIVATE-NETWORK-SENTINEL")
    expect(status).not_to_contain_text("PRIVATE-HTTP-SENTINEL")
    assert page.evaluate("window.errorBodyReads") == 0
    expect(field).to_have_value({"installation": "7.25", "ev": "64", "thermal": "180"}[editor])
    button.click()
    page.wait_for_function("count => window.sourceReads.length === count", arg=2 * expected)
    assert page.evaluate("window.sourceBudgets") == [65000, 65000]
    if scenario != "error":
        page.wait_for_function("() => window.sourceReads.at(-1).aborted")
    expect(button).to_be_enabled()
