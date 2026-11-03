"""Prove calendar ownership and rendering with real Chart.js on desktop and mobile."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize(
    ("start", "end", "hours", "utc", "zone"),
    [
        ("2026-03-27T23:00:00Z", "2026-03-30T22:00:00Z", [24, 23, 24], False, "Europe/Amsterdam"),
        ("2026-10-23T22:00:00Z", "2026-10-26T23:00:00Z", [24, 25, 24], False, "Europe/Amsterdam"),
        ("2026-03-28T00:00:00Z", "2026-03-31T00:00:00Z", [24, 24, 24], True, "Europe/Amsterdam"),
        ("2026-09-05T04:00:00Z", "2026-09-08T03:00:00Z", [24, 23, 24], False, "America/Santiago"),
    ],
)
def test_calendar_bands_follow_installation_days_and_resize(
    app_site, browser, width, start, end, hours, utc, zone
):
    """Installation days span 23/25 actual hours across DST, including skipped midnight. A New York browser cannot move their boundaries. UTC history keeps 24-hour bins."""
    with browser.new_context(
        viewport={"width": width, "height": 700},
        timezone_id="America/New_York",
        locale="en-GB",
    ) as context:
        page = context.new_page()
        page.clock.install(time=datetime.fromisoformat(start) + timedelta(hours=12))
        page.route(
            "**/calendar-preview",
            lambda route: route.fulfill(
                content_type="text/html",
                body="""<link rel="stylesheet" href="/setup/workspace.css">
                <style>body{margin:16px;font-family:system-ui}.chart-wrap{height:280px}</style>
                <h2>Planning</h2><div class="chart-wrap"><canvas id="calendar"></canvas></div>
                <script src="/setup/chart.umd.min.js"></script>""",
            ),
        )
        page.goto(app_site + "/calendar-preview")
        page.evaluate(
            """async ({start,end,utc,zone}) => {
                const {useHomeAssistantTimezone,when} = await import('/setup/display.js');
                const {calendarDays} = await import('/setup/chart_days.js');
                useHomeAssistantTimezone(zone);
                const beginning=Date.parse(start), ending=Date.parse(end);
                window.calendar = new Chart(document.getElementById('calendar'), {
                    type:'line',plugins:[calendarDays],data:{datasets:[{label:'Power',
                        borderColor:'#078b80',pointRadius:0,data:[{x:beginning,y:1},
                        {x:beginning+3600000,y:null},{x:beginning+7200000,y:2},{x:ending,y:4}]}]},
                    options:{responsive:true,maintainAspectRatio:false,animation:false,parsing:false,
                        plugins:{calendarDays:{utc},legend:{display:false}},
                        scales:{x:{type:'linear',min:beginning,max:ending,ticks:{maxTicksLimit:5,maxRotation:0,
                            callback:value=>when(value,{hour:'2-digit',minute:'2-digit'})}},y:{min:0,max:5}}}
                });
            }""",
            {"start": start, "end": end, "utc": utc, "zone": zone},
        )

        def snapshot():
            """Read laid-out headings and actual pixels, rather than source-code fragments."""
            return page.evaluate("""() => {
                const chart=window.calendar, area=chart.chartArea;
                const headings=[...document.querySelector('.chart-day-headings').children];
                return {headings:headings.map(heading=>({label:heading.getAttribute('aria-label'),
                    text:heading.textContent,left:parseFloat(heading.style.left),width:heading.getBoundingClientRect().width,
                    fits:[...heading.children].every(child=>child.scrollWidth<=child.clientWidth),
                    pixel:[...chart.ctx.getImageData(Math.round((parseFloat(heading.style.left)+heading.getBoundingClientRect().width/2)*chart.currentDevicePixelRatio),
                        Math.round((area.bottom-8)*chart.currentDevicePixelRatio),1,1).data]})),
                    left:area.left,right:area.right,top:area.top,data:chart.data.datasets[0].data,
                    overflow:document.documentElement.scrollWidth>innerWidth};
            }""")

        initial = snapshot()
        assert len(initial["headings"]) == 3
        assert "Today:" in initial["headings"][0]["label"]
        assert "Tomorrow:" in initial["headings"][1]["label"]
        assert all(heading["text"] and heading["fits"] for heading in initial["headings"])
        assert initial["headings"][0]["left"] == pytest.approx(initial["left"], abs=0.1)
        assert initial["headings"][-1]["left"] + initial["headings"][-1]["width"] == pytest.approx(
            initial["right"], abs=0.1
        )
        for heading, duration in zip(initial["headings"], hours, strict=True):
            assert heading["width"] / (initial["right"] - initial["left"]) == pytest.approx(
                duration / sum(hours), abs=0.001
            )
            assert heading["pixel"] in ([243, 248, 252, 255], [243, 250, 246, 255])

            def luminance(channels):
                """Use WCAG luminance to protect the existing yellow solar forecast against the actual plot pixels."""
                linear = [
                    channel / 255 / 12.92
                    if channel / 255 <= 0.04045
                    else ((channel / 255 + 0.055) / 1.055) ** 2.4
                    for channel in channels[:3]
                ]
                return sum(
                    value * weight
                    for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
                )

            assert (luminance(heading["pixel"]) + 0.05) / (luminance([181, 137, 0]) + 0.05) >= 3
            assert ("UTC" in heading["label"]) == utc
        assert initial["headings"][0]["pixel"] != initial["headings"][1]["pixel"]
        assert not initial["overflow"]
        zone_name = zone.replace("/", "-")
        mode = "utc" if utc else "local"
        screenshot = (
            Path("results/ci") / f"calendar-days-{width}-{zone_name}-{mode}-{hours[1]}h.png"
        )
        page.screenshot(path=str(screenshot))
        page.set_viewport_size({"width": 768, "height": 700})
        page.wait_for_function(
            "window.calendar.width === document.querySelector('.chart-wrap').clientWidth"
        )
        page.evaluate("window.calendar.update('none')")
        resized = snapshot()
        assert resized["top"] == initial["top"]
        assert resized["data"] == initial["data"]
        assert len(resized["headings"]) == 3
        assert not resized["overflow"]
        page.evaluate("""() => {
            window.calendar.options.scales.x.min += 6*3600000;
            window.calendar.options.scales.x.max -= 6*3600000;
            window.calendar.setDatasetVisibility(0,false);
            window.calendar.update('none');
        }""")
        clipped = snapshot()
        for heading, duration in zip(clipped["headings"], [18, hours[1], 18], strict=True):
            assert heading["width"] / (clipped["right"] - clipped["left"]) == pytest.approx(
                duration / (sum(hours) - 12), abs=0.001
            )
        assert clipped["data"] == initial["data"]
        page.evaluate("window.calendar.data.datasets=[];window.calendar.update('none')")
        assert page.locator(".chart-day-headings > span").count() == 0
        assert page.evaluate("window.calendar.options.layout.padding.top") == 0
        assert page.evaluate("window.calendar.chartArea.top") < initial["top"]
        page.evaluate(
            "data=>{window.calendar.data.datasets=[{data}];window.calendar.update('none')}",
            initial["data"],
        )
        assert page.locator(".chart-day-headings > span").count() == 3
        assert page.evaluate("window.calendar.chartArea.top") == initial["top"]
        page.evaluate("window.calendar.destroy()")
        assert page.locator(".chart-day-headings").count() == 0


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_overview_calendar_headers_refresh_without_duplicates(app_page):
    """Planning charts retain data and headings on refresh. Repeated wintertime hours have distinct ticks and tooltips."""
    from playwright.sync_api import expect

    from tests.frontend.browser.helpers import TOKEN

    page, url = app_page
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(
        page.locator("#price-chart").locator("..").locator(".chart-day-headings > span")
    ).not_to_have_count(0)
    before = page.evaluate(
        "JSON.stringify(Chart.getChart(document.getElementById('price-chart')).data)"
    )
    page.evaluate("""() => {
        const chart=Chart.getChart(document.getElementById('price-chart'));
        for(let index=0;index<3;index++) chart.update('none');
    }""")
    assert page.locator("#price-chart").locator("..").locator(".chart-day-headings").count() == 1
    assert (
        page.evaluate("JSON.stringify(Chart.getChart(document.getElementById('price-chart')).data)")
        == before
    )
    page.locator("#price-chart").locator("..").locator("..").screenshot(
        path=f"results/ci/calendar-overview-{page.viewport_size['width']}.png"
    )
    page.evaluate("""async () => {
        const {useHomeAssistantTimezone}=await import('/setup/display.js');
        useHomeAssistantTimezone('Europe/Amsterdam');
        const chart=Chart.getChart(document.getElementById('price-chart'));
        chart.data.datasets=chart.data.datasets.slice(0,1);
        chart.data.datasets[0].data=[{x:Date.parse('2026-10-25T00:30:00Z'),y:0.1},
            {x:Date.parse('2026-10-25T01:30:00Z'),y:0.2}];
        chart.options.scales.x.min=Date.parse('2026-10-24T22:00:00Z');
        chart.options.scales.x.max=Date.parse('2026-10-25T02:00:00Z');
        chart.update('none');
    }""")
    canvas = page.locator("#price-chart")
    axis_labels = canvas.evaluate(
        "canvas=>Chart.getChart(canvas).scales.x.ticks.map(tick=>tick.label)"
    )
    assert axis_labels[0].startswith("00:00")
    assert len(axis_labels) == len(set(axis_labels))
    assert any("GMT+2" in label for label in axis_labels)
    assert any("GMT+1" in label for label in axis_labels)
    for index, suffix in enumerate(["GMT+2", "GMT+1"]):
        point = page.evaluate(
            "index=>Chart.getChart(document.getElementById('price-chart')).getDatasetMeta(0).data[index].getCenterPoint()",
            index,
        )
        canvas.hover(position={"x": point["x"], "y": point["y"]})
        expect(page.locator("#price-chart-tooltip .chart-tooltip-title")).to_have_text(
            f"10/25/2026, 2:30 AM {suffix}"
        )
