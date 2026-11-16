"""Actual irregular Recorder curves remain distinct from learned averages and conflict fallbacks."""

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import expect

from open_energy_planner.appliances.cycles.model import instant
from tests.app.appliance.raw_support import raw_trace
from tests.app.appliance.support import ENTITY, POWER, readings
from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
@pytest.mark.parametrize(
    "conflict,app_site", [(False, "appliance_followed"), (True, None)], indirect=["app_site"]
)
def test_original_recorder_events_and_conservation_fallback_are_visible(
    app_page, conflict, app_site
):
    """Desktop/mobile show an actual twenty-second 3-kW event, exact timing and an honest retained-profile fallback."""
    page, url = app_page
    catalogue = discover(page, url, publish=False)
    catalogue["entities"].extend(
        [
            {
                "entity_id": ENTITY,
                "name": "Recorded washer",
                "unit": "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
                "device_id": "",
            },
            {
                "entity_id": POWER,
                "name": "Recorded washer power",
                "unit": "W",
                "device_class": "power",
                "state_class": "measurement",
                "device_id": "",
            },
        ]
    )
    catalogue["suggestions"]["consumers"] = [
        {"entity_id": ENTITY, "upstream": None, "power_entity": POWER}
    ]
    other = ENTITY + "_other"
    catalogue["entities"].append(
        {**catalogue["entities"][-2], "entity_id": other, "name": "Other recorded washer"}
    )
    catalogue["suggestions"]["consumers"].append(
        {"entity_id": other, "upstream": None, "power_entity": POWER}
    )
    headers = {"Authorization": "Bearer " + TOKEN}
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=catalogue).ok
    consumers = page.request.get(url + "/v1/consumers?view=settings", headers=headers).json()
    assert page.request.put(
        url + "/v1/consumers",
        headers=headers,
        data={
            "expected_version": consumers["version"],
            "selected": [
                {"entity_id": entity, "weather_sensitive": False} for entity in (ENTITY, other)
            ],
        },
    ).ok
    now = datetime.now(UTC)
    body = raw_trace(conflict=conflict)
    offset = now - instant(body["end"]) - timedelta(seconds=1)
    for field in ("start", "end", "power_reported_at", "energy_reported_at"):
        body[field] = (instant(body[field]) + offset).isoformat()
    for field in ("power_states", "energy_states"):
        for row in body[field]:
            row["at"] = (instant(row["at"]) + offset).isoformat()
    if conflict:
        old = readings(cycles=1)
        shift = (
            now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
            - instant(old[-1]["at"])
            - timedelta(hours=1)
        )
        old = [{**row, "at": (instant(row["at"]) + shift).isoformat()} for row in old]
        assert page.request.post(
            url + "/v1/appliances/history",
            headers=headers,
            data={"entity_id": ENTITY, "power_entity": POWER, "rows": old, "synthetic": True},
        ).ok
    result = page.request.post(url + "/v1/appliances/raw_history", headers=headers, data=body)
    assert result.ok, f"Synthetic raw history upload failed with HTTP {result.status}"
    from copy import deepcopy

    second = deepcopy(body)
    second["entity_id"] = other
    for row in second["power_states"]:
        row["state"] = str(float(row["state"]) * 2)
    for row in second["energy_states"]:
        row["state"] = str(10 + (float(row["state"]) - 10) * 2)
    assert page.request.post(url + "/v1/appliances/raw_history", headers=headers, data=second).ok
    assert (
        page.request.get(url + "/v1/appliances?raw_entity=" + POWER, headers=headers).status == 400
    )
    details = []
    page.add_init_script(f"""(() => {{
        const original = window.fetch.bind(window);
        window.fetch = async (...arguments_) => {{
            const response = await original(...arguments_);
            if (String(arguments_[0]).includes('raw_entity=')) {{
                const decode = response.json.bind(response);
                response.json = async () => {{
                    const data = await decode();
                    if (data.devices.some(device => device.entity_id === {other!r})) {{
                        const revision = data.revision;
                        Object.defineProperty(data, 'revision', {{get() {{
                            queueMicrotask(() => {{ window.releasedRawHandlerCompleted = true; }});
                            return revision;
                        }}}});
                    }}
                    return data;
                }};
            }}
            return response;
        }};
    }})();""")
    page.on(
        "request",
        lambda request: details.append(request.url) if "raw_entity=" in request.url else None,
    )
    page.goto(url + "/appliance-cycles")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#recorded-cycle-status")).to_be_hidden()
    page.get_by_text("Recorded cycle measurements", exact=True).click()
    expect(page.locator("#recorded-cycle")).to_be_visible()
    expect(page.locator("#recorded-cycle-status")).to_contain_text("Actual event times")
    expect(page.locator("#recorded-cycle-status")).to_contain_text(
        "cannot establish a conserved cycle" if conflict else "not continuously followed"
    )
    page.wait_for_function(
        "() => Chart.getChart('recorded-power-chart')?.data.datasets[0].data.length > 3"
    )
    actual = page.evaluate("Chart.getChart('recorded-power-chart').data.datasets[0].data")
    peak = next(index for index, point in enumerate(actual) if point["y"] == 3000)
    assert actual[peak + 1]["x"] - actual[peak]["x"] == pytest.approx(20000)
    assert page.evaluate("Chart.getChart('recorded-power-chart').options.scales.x.type") == "linear"
    page.wait_for_function("""() => ['cycle-chart', 'cycle-energy-chart'].every(id => {
        const chart = Chart.getChart(id), meta = chart.getDatasetMeta(0);
        return meta.data.every((element, index) => Math.abs(element.y - chart.scales.y.getPixelForValue(meta._parsed[index].y)) < .1);
    })""")
    pixels = page.evaluate("""() => {
        const chart = Chart.getChart('recorded-power-chart'), meta = chart.getDatasetMeta(0);
        const index = chart.data.datasets[0].data.findIndex(point => point.y === 3000), ratio = chart.currentDevicePixelRatio;
        const left = meta.data[index].x, right = meta.data[index+1].x, y = meta.data[index].y, found = [];
        for (let x = Math.ceil(chart.chartArea.left); x < chart.chartArea.right; x++) {
            const pixel = chart.ctx.getImageData(Math.round(x*ratio), Math.round(y*ratio), 1, 1).data;
            if (pixel[0] < 20 && pixel[1] > 130 && pixel[1] < 180 && pixel[2] > 90 && pixel[2] < 140) found.push(x);
        }
        return {left, right, found};
    }""")
    assert pixels["found"]
    assert min(pixels["found"]) >= pixels["left"] - 4
    assert max(pixels["found"]) <= pixels["right"] + 4
    if conflict:
        assert max(
            page.evaluate("Chart.getChart('cycle-chart').data.datasets[0].data")
        ) == pytest.approx(1000)
        expect(page.locator("#profile-evidence")).to_contain_text("Energy: 0.50 kWh")
        expect(page.locator("#profile-history")).to_contain_text("Provisional statistical profile")
    else:
        learned = page.evaluate("Chart.getChart('cycle-chart').data.datasets[0].data")
        assert max(point["y"] for point in learned) == 3000
        assert sum(
            page.evaluate("Chart.getChart('cycle-energy-chart').data.datasets[0].data")
        ) == pytest.approx(23 / 45)
        expect(page.locator("#profile-history")).to_contain_text("actual irregular phase durations")
        expect(page.locator("#profile-history")).to_contain_text("Continuously followed profile")
    page.screenshot(
        path=f"results/ci/cycle-recorder-{'conflict' if conflict else 'complete'}-{page.viewport_size['width']}.png",
        full_page=True,
    )
    if not conflict and page.viewport_size["width"] == 1280:
        page.evaluate("Chart.getChart('recorded-power-chart').retainedForTest = true")
        page.wait_for_timeout(10500)
        assert len(details) == 1
        assert page.evaluate("Chart.getChart('recorded-power-chart').retainedForTest")
        pending = []

        def hold_detail(route):
            """Acknowledge interception before changing selection and releasing the stale detail."""
            pending.append(route)
            page.evaluate("window.rawDetailHeld = true")

        page.route("**/v1/appliances?raw_entity=*", hold_detail)
        page.locator("#profile-device").select_option(other)
        expect(page.locator("#profile-device")).to_have_value(other)
        page.wait_for_function("window.rawDetailHeld === true")
        assert len(pending) == 1
        page.locator("#profile-device").select_option(ENTITY)
        with page.expect_response(
            lambda response: "raw_entity=" in response.url and other in response.url
        ):
            pending.pop().continue_()
        # The marker runs after the synchronous consumer finishes evaluating the public revision guard.
        page.wait_for_function("window.releasedRawHandlerCompleted === true")
        assert (
            max(
                point["y"] or 0
                for point in page.evaluate(
                    "Chart.getChart('recorded-power-chart').data.datasets[0].data"
                )
            )
            == 3000
        )
        page.unroute("**/v1/appliances?raw_entity=*")
        page.locator("#profile-device").select_option(other)
        page.wait_for_function(
            "() => Chart.getChart('recorded-power-chart')?.data.datasets[0].data.some(point => point.y === 6000)"
        )
