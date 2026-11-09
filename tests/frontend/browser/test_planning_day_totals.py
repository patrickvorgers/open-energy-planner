"""Exercise daily energy summaries in the three real Planning scenarios."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN


@pytest.mark.parametrize("app_site", ["device_planning"], indirect=True)
def test_all_planning_dates_expose_daily_energy(app_page):
    """Hover and keyboard focus show scenario-specific kWh without summing battery percentages."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    plan_id = page.locator("#plan-version").input_value()
    comparison = page.request.get(
        url + "/v1/plans/" + plan_id + "/comparison",
        headers={"Authorization": "Bearer " + TOKEN},
    ).json()
    snapshots = {
        "plan-chart": comparison["plan"],
        "self-consumption-chart": comparison["scenarios"]["self_consumption"]["chart"],
        "reference-chart": comparison["scenarios"]["without_battery"]["chart"],
    }
    for identity in ("plan-chart", "self-consumption-chart", "reference-chart"):
        card = page.locator("#" + identity).locator("..")
        heading = card.get_by_role("button", name="Energy totals for", exact=False).first
        expect(heading).to_be_visible()
        card.evaluate("element => element.scrollIntoView({block:'center'})")
        heading.hover()
        tooltip = page.locator("#" + identity + "-tooltip")
        expect(tooltip).to_be_visible()
        expect(tooltip).to_contain_text("Energy totals")
        expect(tooltip).to_contain_text("kWh")
        expect(tooltip).not_to_contain_text("SoC")
        expect(tooltip).to_contain_text("Totals cover this complete calendar day.")
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
        directory = Path("results/ci/planning-day-totals")
        directory.mkdir(parents=True, exist_ok=True)
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
        heading = card.get_by_role("button", name="Energy totals for", exact=False).first
        heading.focus()
        tooltip = page.locator("#" + identity + "-tooltip")
        expect(tooltip).to_contain_text("Partial day. Totals cover only the shown planned period.")
        base = tooltip.locator("tr").filter(has_text="Household base: AC").locator("td").nth(1)
        expect(base).to_have_text("-1.26")
        heading.press("Escape")
        legend = page.locator("#" + identity + "-legend")
        legend.get_by_role("checkbox", name="Grid import: AC", exact=True).uncheck()
        heading = card.get_by_role("button", name="Energy totals for", exact=False).first
        heading.focus()
        expect(tooltip.locator("tr").filter(has_text="Grid import: AC")).to_have_count(0)
        heading.press("Escape")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


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
