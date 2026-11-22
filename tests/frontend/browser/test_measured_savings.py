"""Savings A shows one measured cash comparison and a shared responsive calendar."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


def measured_response():
    """Thirty independently specified signed daily cash results include a missing and partial day."""
    first = datetime(2026, 10, 1, tzinfo=UTC)
    chart = []
    for index in range(30):
        start, end = first + timedelta(days=index), first + timedelta(days=index + 1)
        saving = (
            None if index == 5 else -0.35 if index == 13 else round(0.8 + (index % 7) * 0.21, 2)
        )
        actual = None if saving is None else 2.0 + (index % 4) * 0.3
        chart.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "savings": saving,
                "actual_cost": actual,
                "without_battery_cost": None if actual is None else actual + saving,
                "coverage_seconds": 0 if saving is None else 43200 if index == 3 else 86400,
                "estimated_seconds": 900 if index == 2 else 0,
                "breakdown": {
                    "actual": None
                    if actual is None
                    else {"purchase": actual, "sale": 0, "bonus": 0},
                    "without_battery": None
                    if actual is None
                    else {"purchase": actual + saving, "sale": 0, "bonus": 0},
                },
            }
        )
    return {
        "daily": True,
        "currency": "EUR",
        "days": [],
        "cash": {
            "savings": sum(row["savings"] or 0 for row in chart),
            "actual_cost": sum(row["actual_cost"] or 0 for row in chart),
            "without_battery_cost": sum(row["without_battery_cost"] or 0 for row in chart),
            "coverage_seconds": sum(row["coverage_seconds"] for row in chart),
            "estimated_seconds": 900,
            "expected_seconds": 30 * 86400,
            "chart": chart,
        },
    }


@pytest.mark.parametrize("app_site", ["savings", "activity_ingress"], indirect=True)
@pytest.mark.parametrize("aggregation", ["day", "month"])
def test_bar_tooltip_explains_its_own_backend_costs_and_clears_after_refresh(app_page, aggregation):
    """Hover, tap and keyboard show one bar's costs on every viewport, without stale refresh details."""
    page, url = app_page
    data = measured_response()
    if aggregation == "month":
        data["cash"]["chart"] = data["cash"]["chart"][:6]
        for index, chart_bin in enumerate(data["cash"]["chart"]):
            chart_bin.update(
                start=datetime(2026, index + 1, 1, tzinfo=UTC).isoformat(),
                end=datetime(2026, index + 2, 1, tzinfo=UTC).isoformat(),
            )
    data["cash"]["aggregation"] = aggregation
    data["cash"].update(bonus_volume_cap_ignored=True, has_capped_history=True)
    data["cash"]["chart"][2].update(
        actual_cost=4,
        without_battery_cost=12,
        savings=8,
        breakdown={
            "actual": {"purchase": 9.75, "sale": 5.5, "bonus": 0.25},
            "without_battery": {"purchase": 14.25, "sale": 1.75, "bonus": 0.5},
        },
    )
    data["cash"]["chart"][4].update(
        actual_cost=14.56,
        without_battery_cost=10,
        savings=-4.56,
        breakdown={
            "actual": {"purchase": 12.56, "sale": -2, "bonus": 0},
            "without_battery": {"purchase": 10, "sale": 0, "bonus": 0},
        },
    )
    for key in ("actual_cost", "without_battery_cost", "savings"):
        data["cash"][key] = sum(chart_bin[key] or 0 for chart_bin in data["cash"]["chart"])
    data["cash"]["breakdown"] = {
        kind: {
            component: sum(
                (chart_bin["breakdown"][kind] or {}).get(component, 0)
                for chart_bin in data["cash"]["chart"]
            )
            for component in ("purchase", "sale", "bonus")
        }
        for kind in ("actual", "without_battery")
    }
    failed = False

    def respond(route):
        """Keep the chart fixture current and expose a real failed-refresh response."""
        route.fulfill(status=503, json={}) if failed else route.fulfill(json=data)

    page.route("**/v1/savings?**", respond)
    page.goto(url + "/overview#savings")
    if "hassio_ingress" not in url:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#savings-total")).to_contain_text(f"{data['cash']['savings']:.2f}")
    expect(page.locator("#savings-outcome")).to_have_text("Estimated battery saving")
    expect(page.locator("#savings-bonus-basis")).to_be_visible()
    expect(page.locator("#savings-status")).to_contain_text(
        "retained results calculated with the volume cap"
    )
    page.locator("#savings-period").get_by_role("button", name="Choose period").click()
    dialog = page.get_by_role("dialog", name="Choose period")
    dialog.get_by_role("button", name="Custom period", exact=True).click()
    dialog.get_by_label("From", exact=True).fill(data["cash"]["chart"][0]["start"][:10])
    final_day = datetime.fromisoformat(data["cash"]["chart"][-1]["end"]) - timedelta(days=1)
    dialog.get_by_label("To", exact=True).fill(final_day.date().isoformat())
    dialog.get_by_role("button", name="Apply period", exact=True).click()
    canvas = page.locator("#savings-chart")
    expect(dialog).not_to_be_visible()
    directory = Path("results/ci/savings-daily-breakdown")
    directory.mkdir(parents=True, exist_ok=True)
    if aggregation == "day" and "hassio_ingress" not in url:
        page.locator("#savings-breakdown summary").click()
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(
            path=str(directory / f"summary-{page.viewport_size['width']}-standalone.png"),
            full_page=True,
        )
        page.locator("#savings-breakdown summary").click()
    page.get_by_role("button", name="Refresh overview", exact=True).focus()
    canvas.scroll_into_view_if_needed()
    coordinates = canvas.evaluate("""canvas => {
        const bar=Chart.getChart(canvas).getDatasetMeta(0).data[2], bounds=canvas.getBoundingClientRect();
        return {x:bounds.left+bar.x,y:bounds.top+(bar.y+bar.base)/2};
    }""")
    if page.viewport_size["width"] < 500:
        page.touchscreen.tap(coordinates["x"], coordinates["y"])
    else:
        page.mouse.move(coordinates["x"], coordinates["y"])
    tooltip = page.get_by_role("tooltip")
    expect(tooltip).to_be_visible()
    table = tooltip.get_by_role("table", name="Savings breakdown for selected bar")
    expect(table.get_by_role("row", name="Import cost 9.75 EUR 14.25 EUR")).to_be_visible()
    expect(table.get_by_role("row", name="Export income 5.50 EUR 1.75 EUR")).to_be_visible()
    expect(table.get_by_role("row", name="Bonus income 0.25 EUR 0.50 EUR")).to_be_visible()
    expect(table.get_by_role("row", name="Net electricity cost 4.00 EUR 12.00 EUR")).to_be_visible()
    expect(tooltip).to_contain_text("Saving: 8.00 EUR")
    expect(tooltip).to_contain_text("Export bonus volume caps are ignored in these estimates.")
    if page.viewport_size["width"] >= 500:
        bounds = tooltip.bounding_box()
        page.mouse.move(
            bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2, steps=24
        )
        expect(tooltip).to_be_visible()
        expect(tooltip).to_contain_text("Saving: 8.00 EUR")
        page.mouse.move(1, 1)
        expect(tooltip).not_to_be_visible()
        page.mouse.move(coordinates["x"], coordinates["y"])
        expect(tooltip).to_contain_text("Saving: 8.00 EUR")
        page.keyboard.press("Escape")
        expect(tooltip).not_to_be_visible()
        expect(page.get_by_role("button", name="Refresh overview", exact=True)).to_be_focused()
        page.mouse.move(1, 1)
        page.mouse.move(coordinates["x"], coordinates["y"])
        expect(tooltip).to_be_visible()
    assert tooltip.evaluate(
        "element => {const bounds=element.getBoundingClientRect();return bounds.left>=0 && bounds.right<=innerWidth && bounds.top>=0 && bounds.bottom<=innerHeight && element.scrollWidth<=element.clientWidth;}"
    )
    page.screenshot(
        path=str(
            directory
            / f"{aggregation}-{page.viewport_size['width']}-{'ingress' if 'hassio_ingress' in url else 'standalone'}.png"
        ),
        full_page=False,
    )
    canvas.focus()
    page.keyboard.press("Escape")
    expect(tooltip).not_to_be_visible()
    page.keyboard.press("Home")
    expect(tooltip.get_by_role("row", name="Export income 0.00 EUR 0.00 EUR")).to_be_visible()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    expect(tooltip).to_contain_text("Saving: 8.00 EUR")
    page.keyboard.press("ArrowRight")
    expect(tooltip).not_to_contain_text("9.75 EUR")
    page.keyboard.press("ArrowRight")
    expect(tooltip).to_contain_text("Saving: -4.56 EUR")
    expect(tooltip.get_by_role("row", name="Export income -2.00 EUR 0.00 EUR")).to_be_visible()
    data["cash"]["chart"][3]["breakdown"]["actual"] = None
    data["cash"].update(bonus_volume_cap_ignored=False, has_capped_history=False)
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(tooltip).not_to_be_visible()
    expect(canvas).to_have_attribute("tabindex", "0")
    expect(page.locator("#savings-chart-tooltip")).to_have_text("")
    expect(canvas).to_have_accessible_description("")
    canvas.focus()
    page.keyboard.press("Home")
    for _ in range(3):
        page.keyboard.press("ArrowRight")
    expect(tooltip.get_by_role("row", name="Import cost Unavailable", exact=False)).to_be_visible()
    expect(tooltip).to_contain_text("same comparable time")
    expect(tooltip).not_to_contain_text("Export bonus volume caps are ignored")
    expect(page.locator("#savings-bonus-basis")).not_to_be_visible()
    expect(tooltip).not_to_contain_text("9.75 EUR")
    if page.viewport_size["width"] >= 500:
        page.keyboard.press("PageDown")
        expect(tooltip).not_to_be_visible()
        expect(canvas).to_have_accessible_description("")
    failed = True
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#savings-total")).to_have_text("Not available")
    expect(tooltip).not_to_be_visible()
    expect(canvas).to_have_accessible_description("")


@pytest.mark.parametrize("app_site", ["savings", "activity_ingress"], indirect=True)
def test_savings_a_uses_matched_cash_totals_and_a_custom_calendar(app_page):
    """Desktop/mobile show the same signed results, custom dates and keyboard chart access without simulation controls."""
    page, url = app_page
    data = measured_response()
    page.route("**/v1/savings?**", lambda route: route.fulfill(json=data))
    page.goto(url + "/overview#savings")
    if "hassio_ingress" not in url:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#savings-total")).to_contain_text(f"{data['cash']['savings']:.2f}")
    expect(page.locator("#savings-status")).to_contain_text("Partial period")
    expect(page.get_by_text("Evaluation version", exact=True)).to_have_count(0)
    page.locator("#savings-period").get_by_role("button", name="Choose period").click()
    dialog = page.get_by_role("dialog", name="Choose period")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name="This month", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="Custom period", exact=True).click()
    dialog.get_by_label("From", exact=True).fill("2026-10-01")
    dialog.get_by_label("To", exact=True).fill("2026-10-30")
    with page.expect_request(
        lambda request: "/v1/savings?" in request.url and "2026-10-01" in request.url
    ):
        dialog.get_by_role("button", name="Apply period", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.locator("#savings-chart")).to_have_attribute("tabindex", "0")
    page.locator("#savings-chart").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#savings-selected")).not_to_be_empty()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("heading", name="Savings", exact=True).click()
    page.evaluate("window.scrollTo(0, 0)")
    directory = Path("results/ci/measured-savings")
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(
            directory
            / f"savings-{page.viewport_size['width']}-{'ingress' if 'hassio_ingress' in url else 'standalone'}.png"
        ),
        full_page=True,
    )
    page.locator("#savings-period").get_by_role("button", name="Choose period").click()
    page.screenshot(
        path=str(
            directory
            / f"calendar-{page.viewport_size['width']}-{'ingress' if 'hassio_ingress' in url else 'standalone'}.png"
        ),
        full_page=False,
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_negative_savings_and_failed_refresh_never_show_stale_money(app_page):
    """A negative result is an extra cost, and a failed newer request clears all previous amounts."""
    page, url = app_page
    data = measured_response()
    data["cash"].update(
        savings=-4.56,
        actual_cost=14.56,
        without_battery_cost=10,
        breakdown={
            "actual": {"purchase": 14.56, "sale": 0, "bonus": 0},
            "without_battery": {"purchase": 10, "sale": 0, "bonus": 0},
        },
    )
    failed = False
    page.route(
        "**/v1/savings?**",
        lambda route: (
            route.fulfill(status=503, body="Unavailable") if failed else route.fulfill(json=data)
        ),
    )
    page.goto(url + "/overview#savings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#savings-outcome")).to_have_text("Extra electricity cost")
    expect(page.locator("#savings-total")).to_contain_text("4.56")
    page.get_by_text("Savings breakdown", exact=True).click()
    expect(page.locator("#savings-breakdown-total")).to_have_text("Saving: -4.56 EUR")
    failed = True
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#savings-total")).to_have_text("Not available")
    expect(page.locator("#savings-actual")).to_have_text("Unavailable")
    expect(page.locator("#savings-breakdown-total")).to_have_text("Savings unavailable")
    expect(page.locator("#savings-breakdown-values")).not_to_contain_text("14.56")
    expect(page.locator("#savings-status")).to_contain_text("could not be loaded")
    expect(page.locator("#savings-chart")).to_have_attribute("tabindex", "-1")
    assert page.locator("#savings-chart").evaluate("canvas => canvas.onkeydown") is None


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_no_completed_savings_day_disables_selection_until_a_day_completes(app_page):
    """First-day installations show no active date selection, then recover on a later refresh."""
    page, url = app_page
    data = measured_response()
    data["calendar"] = {"has_completed_days": False, "min_day": None, "max_day": None}
    page.route("**/v1/savings?**", lambda route: route.fulfill(json=data))
    page.goto(url + "/overview#savings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#savings-period .period-trigger")).to_be_disabled()
    expect(page.locator("#savings-period-label")).to_have_count(0)
    expect(page.locator("#savings-status")).to_contain_text("after the first measured day")
    data["calendar"] = {
        "has_completed_days": True,
        "min_day": "2026-10-01",
        "max_day": "2026-10-30",
    }
    data["cash"]["aggregation"] = "month"
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#savings-period .period-trigger")).to_be_enabled()
    expect(page.locator("#savings-daily-heading")).to_have_text("Savings by month")


@pytest.mark.parametrize("app_site", ["savings", "activity_ingress"], indirect=True)
@pytest.mark.parametrize("aggregation", ["day", "month"])
def test_savings_breakdown_and_bar_labels_follow_the_selected_period(app_page, aggregation):
    """Actual painted values and calendar labels stay aligned with six signed bars on desktop/mobile and ingress."""
    page, url = app_page
    data = measured_response()
    savings = [1.90, 2.40, -0.35, 3.10, 2.65, 2.70]
    starts = (
        [datetime(2026, month, 1, tzinfo=UTC) for month in range(1, 8)]
        if aggregation == "month"
        else [datetime(2026, 10, 12, tzinfo=UTC) + timedelta(days=index) for index in range(7)]
    )
    data["cash"].update(
        savings=12.40,
        coverage_seconds=(starts[-1] - starts[0]).total_seconds(),
        expected_seconds=(starts[-1] - starts[0]).total_seconds(),
        estimated_seconds=0,
        actual_cost=20.50,
        without_battery_cost=32.90,
        aggregation=aggregation,
        breakdown={
            "actual": {"purchase": 25.50, "sale": 4, "bonus": 1},
            "without_battery": {"purchase": 38.90, "sale": 5, "bonus": 1},
        },
        chart=[
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "savings": value,
                "coverage_seconds": (end - start).total_seconds(),
                "estimated_seconds": 0,
            }
            for start, end, value in zip(starts[:-1], starts[1:], savings, strict=True)
        ],
    )
    page.route("**/v1/savings?**", lambda route: route.fulfill(json=data))
    page.goto(url + "/overview#savings")
    if "hassio_ingress" not in url:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#savings-total")).to_contain_text("12.40")
    page.locator("#savings-period").get_by_role("button", name="Choose period").click()
    dialog = page.get_by_role("dialog", name="Choose period")
    dialog.get_by_role("button", name="Custom period", exact=True).click()
    dialog.get_by_label("From", exact=True).fill(starts[0].date().isoformat())
    dialog.get_by_label("To", exact=True).fill((starts[-1] - timedelta(days=1)).date().isoformat())
    dialog.get_by_role("button", name="Apply period", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.locator("#savings-period-label")).to_have_count(0)
    expect(page.locator(".savings-cost-label").first).to_contain_text("With your battery")
    page.get_by_text("Savings breakdown", exact=True).click()
    table = page.get_by_role("table", name="Savings breakdown")
    expect(table.get_by_role("row", name="Import cost 25.50 EUR 38.90 EUR")).to_be_visible()
    expect(table.get_by_role("row", name="Export income 4.00 EUR 5.00 EUR")).to_be_visible()
    expect(table.get_by_role("row", name="Bonus income 1.00 EUR 1.00 EUR")).to_be_visible()
    expect(
        table.get_by_role("row", name="Net electricity cost 20.50 EUR 32.90 EUR")
    ).to_be_visible()
    expect(page.locator("#savings-breakdown-total")).to_have_text("Saving: 12.40 EUR")
    painted = page.locator("#savings-chart").evaluate("""canvas => {
        const chart=Chart.getChart(canvas), context=chart.ctx, original=context.fillText, calls=[];
        context.fillText=function(text,x,y,...rest){calls.push({text:String(text),x,y,width:this.measureText(String(text)).width});return original.call(this,text,x,y,...rest);};
        chart.update('none');context.fillText=original;
        return {calls, bars:chart.getDatasetMeta(0).data.map(bar=>({x:bar.x,y:bar.y,base:bar.base})), ticks:chart.scales.x.ticks, width:canvas.clientWidth};
    }""")
    labels = []
    for amount, bar in zip(savings, painted["bars"], strict=True):
        matches = [
            call
            for call in painted["calls"]
            if call["text"] == f"{amount:.2f}" and abs(call["x"] - bar["x"]) < 0.1
        ]
        assert matches and matches[-1]["y"] < min(bar["y"], bar["base"])
        labels.append(matches[-1])
    for left, right in pairwise(labels):
        assert left["x"] + left["width"] / 2 < right["x"] - right["width"] / 2
    assert len(painted["ticks"]) >= 2
    centers = [(start.timestamp() + end.timestamp()) * 500 for start, end in pairwise(starts)]
    assert all(
        any(abs(tick["value"] - center) < 1 for center in centers) for tick in painted["ticks"]
    )
    assert all(tick["label"] for tick in painted["ticks"])
    if page.viewport_size["width"] >= 1000:
        assert len(painted["ticks"]) == 6
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("#savings-breakdown .table-scroll").evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )
    directory = Path("results/ci/savings-breakdown")
    directory.mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo(0,0)")
    if aggregation == "day" and "hassio_ingress" in url:
        page.locator("#savings-breakdown > summary").click()
        page.screenshot(
            path=str(directory / f"overview-{page.viewport_size['width']}-ingress.png"),
            full_page=True,
        )
        page.locator("#savings-breakdown > summary").click()
    page.screenshot(
        path=str(
            directory
            / f"{aggregation}-{page.viewport_size['width']}-{'ingress' if 'hassio_ingress' in url else 'standalone'}.png"
        ),
        full_page=True,
    )
    if aggregation == "day" and page.viewport_size["width"] == 1280 and "hassio_ingress" in url:
        for width in (1024, 768, 320):
            page.set_viewport_size({"width": width, "height": 1000})
            page.wait_for_function(
                "Math.abs(Chart.getChart(document.getElementById('savings-chart')).width - document.getElementById('savings-chart').parentElement.clientWidth) < 1"
            )
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.locator("#savings-breakdown .table-scroll").evaluate(
                "element => element.scrollWidth <= element.clientWidth"
            )
            if width == 320:
                assert (
                    page.locator("#savings-chart").evaluate(
                        "canvas => Chart.getChart(canvas).scales.x.ticks.length"
                    )
                    < 6
                )
            page.screenshot(path=str(directory / f"day-{width}-ingress.png"), full_page=True)
    data["cash"]["breakdown"]["actual"] = None
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#savings-breakdown-total")).to_have_text("Saving: 12.40 EUR")
    expect(table.get_by_role("row", name="Import cost Unavailable 38.90 EUR")).to_be_visible()
    expect(page.locator("#savings-breakdown-status")).to_contain_text("same comparable time")
    if aggregation == "day" and "hassio_ingress" in url:
        page.evaluate("window.scrollTo(0,0)")
        page.screenshot(
            path=str(directory / f"unavailable-{page.viewport_size['width']}-ingress.png"),
            full_page=True,
        )
