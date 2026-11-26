"""Exercise daily energy summaries in the three real Planning scenarios."""

from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN, open_chart_series, wait_for_product_reads


def scenario_snapshots(page, url):
    """Read original per-scenario kWh as an independent oracle for displayed daily totals."""
    plan_id = page.locator("#plan-version").input_value()
    comparison = page.request.get(
        url + "/v1/plans/" + plan_id + "/comparison",
        headers={"Authorization": "Bearer " + TOKEN},
    ).json()
    return {
        "plan-chart": comparison["plan"],
        "self-consumption-chart": comparison["scenarios"]["self_consumption"]["chart"],
        "reference-chart": comparison["scenarios"]["without_battery"]["chart"],
    }


def expect_day_benefit(page, url, tooltip, chart, start, end):
    """Compare the displayed amount with the owning scenario's original financial slice."""
    line = tooltip.locator(".chart-tooltip-day-benefit")
    if chart == "reference-chart":
        expect(line).to_have_count(0)
        return
    plan_id = page.locator("#plan-version").input_value()
    result = page.request.get(
        url + f"/v1/plans/{plan_id}/comparison",
        params={"start": start.isoformat(), "end": end.isoformat()},
        headers={"Authorization": "Bearer " + TOKEN},
    )
    assert result.ok
    scenario = "optimizer" if chart == "plan-chart" else "self_consumption"
    benefit = result.json()["scenarios"][scenario]["benefit_vs_without_battery"]
    if abs(benefit) < 0.005:
        benefit = 0
    expect(line).to_have_text(f"Expected benefit versus without battery: {benefit:,.2f} EUR")


@pytest.mark.parametrize("app_site", ["device_planning"], indirect=True)
def test_all_planning_dates_expose_daily_energy(app_page):
    """Hover and keyboard focus show scenario-specific kWh without summing battery percentages."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    snapshots = scenario_snapshots(page, url)
    directory = Path("results/ci/planning-day-totals")
    directory.mkdir(parents=True, exist_ok=True)
    for identity in ("plan-chart", "self-consumption-chart", "reference-chart"):
        card = page.locator("#" + identity).locator("..")
        heading = card.get_by_role("button", name="Expected energy totals for", exact=False).first
        expect(heading).to_be_visible()
        card.evaluate("element => element.scrollIntoView({block:'center'})")
        heading.hover()
        tooltip = page.locator("#" + identity + "-tooltip")
        expect(tooltip).to_be_visible()
        expect(tooltip.locator(".chart-tooltip-title")).to_have_text(
            "Expected energy totals: 11/4/2026"
        )
        expect(tooltip.locator(".chart-tooltip-total td:first-child")).to_have_text(
            ["Total expected production", "Total expected energy use"]
        )
        expect(tooltip).to_contain_text("kWh")
        expect(tooltip).not_to_contain_text("SoC")
        expect(tooltip).to_contain_text("Totals cover this complete calendar day.")
        expect(tooltip.locator(".chart-tooltip-period")).to_have_count(0)
        expect_day_benefit(page, url, tooltip, identity, START, START + timedelta(days=1))
        snapshot = snapshots[identity]
        # The fixture starts at UTC midnight. Read independently archived kWh, not chart means.
        expected_import = sum(
            flow["grid_import"]
            for interval, flow in zip(
                snapshot["input_snapshot"]["problem"]["intervals"],
                snapshot["expected_flows"],
                strict=True,
            )
            if datetime.fromisoformat(interval["start"]) < START + timedelta(days=1)
        )
        displayed = tooltip.locator("tr").filter(has_text="Grid import: AC").locator("td").nth(1)
        assert float(displayed.inner_text().replace(",", "")) == pytest.approx(
            expected_import, abs=0.00051
        )
        base = tooltip.locator("tr").filter(has_text="Household base: AC").locator("td").nth(1)
        expect(base).to_have_text("-17.54")
        tooltip_bounds = tooltip.bounding_box()
        date_bounds = heading.bounding_box()
        assert (
            tooltip_bounds["y"] + tooltip_bounds["height"] <= date_bounds["y"] + 1
            or tooltip_bounds["y"] >= date_bounds["y"] + date_bounds["height"] - 1
        )
        assert tooltip_bounds["x"] >= 0
        assert tooltip_bounds["x"] + tooltip_bounds["width"] <= page.viewport_size["width"]
        page.screenshot(path=str(directory / f"{identity}-{page.viewport_size['width']}.png"))
        heading.focus()
        page.evaluate("id => Chart.getChart(document.getElementById(id)).draw()", identity)
        expect(heading).to_be_focused()
        expect(tooltip).to_be_visible()
        heading.press("Escape")
        expect(tooltip).to_be_hidden()
        if page.viewport_size["width"] < 500:
            page.mouse.move(0, 0)
            heading.tap()
            expect(tooltip).to_be_visible()
            heading.tap()
            expect(tooltip).to_be_hidden()
        page.mouse.move(0, 0)

    page.get_by_role("button", name="Next 6 hours", exact=True).click()
    for identity in snapshots:
        card = page.locator("#" + identity).locator("..")
        card.evaluate("element => element.scrollIntoView({block:'center'})")
        heading = card.get_by_role("button", name="Expected energy totals for", exact=False).first
        heading.focus()
        tooltip = page.locator("#" + identity + "-tooltip")
        expect(tooltip.locator(".chart-tooltip-title")).to_have_text(
            "Expected energy totals: 11/4/2026"
        )
        expect(tooltip).to_contain_text("Partial day. Totals cover only the shown planned period.")
        expect_day_benefit(page, url, tooltip, identity, START, START + timedelta(hours=6))
        expect(tooltip.locator(".chart-tooltip-period")).to_be_in_viewport()
        expect(tooltip.locator(".chart-tooltip-period")).to_have_text("From 00:00 to 06:00.")
        page.screenshot(
            path=str(directory / f"{identity}-partial-{page.viewport_size['width']}.png")
        )
        base = tooltip.locator("tr").filter(has_text="Household base: AC").locator("td").nth(1)
        expect(base).to_have_text("-1.26")
        totals = tooltip.locator(".chart-tooltip-total .chart-tooltip-value").all_text_contents()
        assert len(totals) == 2
        assert float(totals[0].replace(",", "")) == pytest.approx(
            -float(totals[1].replace(",", "")), abs=0.001
        )
        heading.press("Escape")
        legend = open_chart_series(page, identity)
        legend.get_by_role("checkbox", name="Grid import: AC", exact=True).uncheck()
        legend.get_by_text("Chart series", exact=True).press("Escape")
        heading = card.get_by_role("button", name="Expected energy totals for", exact=False).first
        heading.focus()
        expect(tooltip.locator("tr").filter(has_text="Grid import: AC")).to_have_count(0)
        expect(tooltip.locator(".chart-tooltip-total .chart-tooltip-value")).to_have_text(totals)
        expect(tooltip).to_contain_text("Totals include all flows, including hidden series.")
        heading.press("Escape")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("app_site", ["device_planning"], indirect=True)
@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("hover_before_focus", [False, True], ids=["focus", "hover_then_focus"])
def test_delayed_day_benefit_stays_with_its_own_date(app_page, available, hover_before_focus):
    """A late first-day result leaves a second-day loss or missing comparison intact."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    tomorrow = START + timedelta(days=1)
    first_day = (START, tomorrow)
    second_day = (tomorrow, tomorrow + timedelta(days=1))
    queued = []

    def comparison_window(request_url):
        """Identify comparison requests by both bounds, ignoring full-plan and other reads."""
        parsed = urlsplit(request_url)
        query = parse_qs(parsed.query)
        if not parsed.path.endswith("/comparison") or not {"start", "end"} <= query.keys():
            return None
        return tuple(datetime.fromisoformat(query[bound][0]) for bound in ("start", "end"))

    def comparison(route):
        """Hold the first date and supply a distinct outcome for the selected second day."""
        bounds = comparison_window(route.request.url)
        if bounds == first_day:
            queued.append(route)
        elif bounds != second_day:
            route.fallback()
        elif available:
            route.fulfill(json={"scenarios": {"optimizer": {"benefit_vs_without_battery": -1.25}}})
        else:
            route.fulfill(status=503)

    page.route("**/comparison?start=**", comparison)
    page.mouse.move(0, 0)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_have_text("Connected")
    expect(page.locator("#ems-costs")).to_contain_text("Expected benefit versus without battery")
    card = page.locator("#plan-chart").locator("..")
    first = card.get_by_role(
        "button", name="Expected energy totals for Today: Wednesday, November 4, 2026"
    )
    second = card.get_by_role(
        "button", name="Expected energy totals for Tomorrow: Thursday, November 5, 2026"
    )
    tooltip = page.locator("#plan-chart-tooltip")
    title = tooltip.locator(".chart-tooltip-title")
    line = tooltip.locator(".chart-tooltip-day-benefit")
    with page.expect_request(lambda request: comparison_window(request.url) == first_day):
        if hover_before_focus:
            # Isolate request ordering from pointer hit-testing, covered by the real-hover test.
            first.dispatch_event("mouseenter")
        else:
            first.focus()
    expect(title).to_have_text("Expected energy totals: 11/4/2026")
    expect(line).to_contain_text("Loading")
    first.focus()
    expect(line).to_contain_text("Loading")
    second.focus()
    amount = "-1.25 EUR" if available else "Unavailable"
    expect(title).to_have_text("Expected energy totals: 11/5/2026")
    expect(line).to_have_text(f"Expected benefit versus without battery: {amount}")
    [delayed] = queued
    with page.expect_response(lambda response: response.url == delayed.request.url):
        delayed.fulfill(json={"scenarios": {"optimizer": {"benefit_vs_without_battery": 99}}})
    wait_for_product_reads(page)
    expect(title).to_have_text("Expected energy totals: 11/5/2026")
    expect(line).to_have_text(f"Expected benefit versus without battery: {amount}")


@pytest.mark.parametrize("app_site", ["device_planning"], indirect=True)
@pytest.mark.parametrize("identity", ["plan-chart", "self-consumption-chart", "reference-chart"])
def test_switching_planning_dates_keeps_the_new_summary_open(app_page, identity):
    """A click or tap changes date ownership without immediately dismissing the new day's totals."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    snapshots = scenario_snapshots(page, url)
    snapshot = snapshots[identity]
    card = page.locator("#" + identity).locator("..")
    card.evaluate("element => element.scrollIntoView({block:'center'})")
    dates = card.get_by_role("button", name="Expected energy totals for", exact=False)
    first, second = dates.nth(0), dates.nth(1)
    tooltip = page.locator("#" + identity + "-tooltip")
    first.focus()
    expect(tooltip).to_be_visible()
    if page.viewport_size["width"] < 500:
        second.tap()
    else:
        second.click()
    expect(tooltip).to_be_visible()
    expect(tooltip.locator(".chart-tooltip-title")).to_have_text(
        "Expected energy totals: 11/5/2026"
    )
    expect(first).not_to_have_attribute("aria-describedby", identity + "-tooltip")
    expect(second).to_have_attribute("aria-describedby", identity + "-tooltip")
    expect_day_benefit(
        page, url, tooltip, identity, START + timedelta(days=1), START + timedelta(days=2)
    )
    expected_import = sum(
        flow["grid_import"]
        for interval, flow in zip(
            snapshot["input_snapshot"]["problem"]["intervals"],
            snapshot["expected_flows"],
            strict=True,
        )
        if START + timedelta(days=1)
        <= datetime.fromisoformat(interval["start"])
        < START + timedelta(days=2)
    )
    displayed = tooltip.locator("tr").filter(has_text="Grid import: AC").locator("td").nth(1)
    assert float(displayed.inner_text().replace(",", "")) == pytest.approx(
        expected_import, abs=0.00051
    )
    if page.viewport_size["width"] < 500:
        second.tap()
    else:
        second.click()
    expect(tooltip).to_be_hidden()
    expect(second).not_to_have_attribute("aria-describedby", identity + "-tooltip")
    if page.viewport_size["width"] < 500:
        first.tap()
    else:
        first.click()
    expect(tooltip).to_be_visible()
    expect(first).to_have_attribute("aria-describedby", identity + "-tooltip")
    expect(second).not_to_have_attribute("aria-describedby", identity + "-tooltip")
    first.press("Tab")
    expect(second).to_be_focused()
    expect(tooltip).to_be_visible()
    second.press("Escape")
    expect(tooltip).to_be_hidden()
    page.mouse.move(0, 0)


@pytest.mark.parametrize("app_site", ["device_planning"], indirect=True)
@pytest.mark.parametrize("identity", ["plan-chart", "self-consumption-chart", "reference-chart"])
def test_focusing_an_offscreen_date_keeps_its_summary(app_page, identity):
    """Native focus scrolling keeps the date's summary and description after queued scroll events."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    card = page.locator("#" + identity).locator("..")
    date = card.get_by_role("button", name="Expected energy totals for", exact=False).nth(1)
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    page.wait_for_function(
        """() => scrollY >= document.documentElement.scrollHeight - innerHeight - 1"""
    )
    assert date.bounding_box()["y"] < 0
    date.focus()
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    expect(date).to_be_focused()
    bounds = date.bounding_box()
    assert 0 <= bounds["y"] < page.viewport_size["height"]
    tooltip = page.locator("#" + identity + "-tooltip")
    expect(tooltip).to_be_visible()
    expect(date).to_have_attribute("aria-describedby", identity + "-tooltip")
    expect(tooltip.locator(".chart-tooltip-title")).to_have_text(
        "Expected energy totals: 11/5/2026"
    )
    base = tooltip.locator("tr").filter(has_text="Household base: AC").locator("td").nth(1)
    expect(base).to_have_text("-17.54")
    page.mouse.move(page.viewport_size["width"] - 20, page.viewport_size["height"] / 2)
    page.mouse.wheel(0, page.viewport_size["height"] * 2)
    page.wait_for_function(
        "id => document.getElementById(id).parentElement.querySelectorAll('.chart-day-summary')[1].getBoundingClientRect().bottom < 0",
        arg=identity,
    )
    expect(tooltip).to_be_hidden()
    expect(date).not_to_have_attribute("aria-describedby", identity + "-tooltip")
    expect(date).to_be_focused()


@pytest.mark.parametrize(
    "day,zone,hours",
    [
        ("2026-03-29", "Europe/Amsterdam", 23),
        ("2026-10-25", "Europe/Amsterdam", 25),
        ("2026-09-06", "America/Santiago", 23),
        ("2026-04-05", "Australia/Lord_Howe", 24.5),
    ],
)
def test_daily_energy_uses_actual_local_day_duration(app_site, browser, day, zone, hours):
    """A constant 2 kW integrates to actual local-day kWh, irrespective of the browser timezone."""
    with browser.new_context(timezone_id="America/New_York") as context:
        page = context.new_page()
        page.goto(app_site + "/overview")
        actual = page.evaluate(
            """async ({day,zone}) => {
                const {useHomeAssistantTimezone,nextDate} = await import('/setup/display.js');
                const {calendarDayStart} = await import('/setup/chart_days.js');
                const {balancePeriodEnergy} = await import('/setup/balance.js');
                useHomeAssistantTimezone(zone);
                const start=calendarDayStart(day), end=calendarDayStart(nextDate(day));
                const intervals=[];
                for (let cursor=start-3600000;cursor<end+3600000;cursor+=3600000)
                    intervals.push({start:cursor,end:cursor+3600000,value:2});
                const original=JSON.stringify(intervals);
                return {energy:balancePeriodEnergy(intervals,start,end),
                    unchanged:original===JSON.stringify(intervals)};
            }""",
            {"day": day, "zone": zone},
        )
        assert actual == {"energy": 2 * hours, "unchanged": True}


@pytest.mark.parametrize("fault", ["zero", "missing", "gap", "overlap", "partial"])
def test_daily_energy_keeps_missing_evidence_distinct_from_zero(app_site, browser, fault):
    """Partial edges retain their actual energy. Missing, gapped and conflicting intervals fail closed."""
    with browser.new_context() as context:
        page = context.new_page()
        page.goto(app_site + "/overview")
        energy = page.evaluate(
            """async fault => {
                const {balancePeriodEnergy} = await import('/setup/balance.js');
                const rows=[{start:0,end:900000,value:2},{start:900000,end:1800000,value:-4}];
                if (fault==='zero') rows.forEach(row=>row.value=0);
                if (fault==='missing') rows[1].value=null;
                if (fault==='gap') rows[1].start+=60000;
                if (fault==='overlap') rows[1].start-=60000;
                if (fault==='partial') {
                    rows[0].coveredStart=300000; rows[1].coveredEnd=1500000;
                    return balancePeriodEnergy(rows,300000,1500000);
                }
                return balancePeriodEnergy(rows,0,1800000);
            }""",
            fault,
        )
        expected = 0 if fault == "zero" else -1 / 3 if fault == "partial" else None
        if expected is None:
            assert energy is None
        else:
            assert energy == pytest.approx(expected)


@pytest.mark.parametrize("app_site", ["device_planning"], indirect=True)
def test_interval_totals_include_filtered_appliances_in_each_scenario(app_page):
    """All three scenarios show full-precision power totals despite filtered appliance rows."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    for identity in ("plan-chart", "self-consumption-chart", "reference-chart"):
        page.locator("#" + identity).scroll_into_view_if_needed()
        # Inspect a real quarter bar, retaining devices below the legend threshold.
        canvas = page.locator("#" + identity)
        quarter = page.evaluate(
            """id => {
            const chart = Chart.getChart(document.getElementById(id));
            const bars = chart.data.datasets.filter(dataset => dataset.type === 'bar');
            return {point: chart.getDatasetMeta(0).data[0].getCenterPoint(),
                totals: ['Production', 'Consumption'].map(group => bars
                    .filter(dataset => dataset.tooltipGroup === group)
                    .reduce((sum, dataset) => sum + dataset.data[0].y, 0)),
                hidden: bars.some(dataset => dataset.data[0].y !== 0 && !chart.isDatasetVisible(chart.data.datasets.indexOf(dataset)))};
        }""",
            identity,
        )
        assert quarter["hidden"]
        canvas.hover(position={"x": quarter["point"]["x"], "y": quarter["point"]["y"]})
        tooltip = page.locator("#" + identity + "-tooltip")
        expect(tooltip).to_be_visible()
        assert [
            float(value.replace(",", ""))
            for value in tooltip.locator(
                ".chart-tooltip-total .chart-tooltip-value"
            ).all_text_contents()
        ] == pytest.approx(quarter["totals"], abs=0.00051)
        expect(tooltip.locator(".chart-tooltip-total .chart-tooltip-unit")).to_have_text(
            ["kW", "kW"]
        )
        directory = Path("results/ci/planning-tooltip-totals")
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f"{identity}-{page.viewport_size['width']}.png"))
