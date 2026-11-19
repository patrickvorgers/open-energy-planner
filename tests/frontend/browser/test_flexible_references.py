"""Planning reference bars and costs follow their own unoptimized service on desktop and mobile."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads


@pytest.mark.parametrize("app_site", ["unoptimized_loads"], indirect=True)
def test_reference_charges_ready_car_before_optimizer_and_explains_policy(app_page):
    """A ready evening car charges earlier in both references, with real matching costs and full-day charts."""
    page, url = app_page
    page.clock.install(time=START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    wait_for_product_reads(page)
    expect(page.locator("#reference-description")).to_contain_text(
        "Without battery or price-based load shifting"
    )
    headers = {"Authorization": "Bearer " + TOKEN}
    identity = page.request.get(url + "/v1/plans?limit=1", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    data = page.request.get(url + f"/v1/plans/{identity}/comparison", headers=headers).json()
    optimized = data["plan"]["expected_flows"]
    baseline = data["scenarios"]["without_battery"]["chart"]["expected_flows"]
    self_consumption = data["scenarios"]["self_consumption"]["chart"]["expected_flows"]

    def first_charge(rows):
        """Find the first materially charged interval from real scenario output."""
        return next(index for index, row in enumerate(rows) if row["ev"][0]["ac_kwh"] > 0.001)

    assert first_charge(baseline) < first_charge(optimized)
    assert first_charge(self_consumption) == first_charge(baseline)
    assert data["scenarios"]["without_battery"]["comparison"]["reference_costs"] is not None
    page.locator('[data-hours="0"]').click()
    wait_for_product_reads(page)
    bars = page.evaluate("""() => ['plan-chart','reference-chart'].map(id => {
        const chart = Chart.getChart(document.getElementById(id));
        return chart.data.datasets.find(row => row.type === 'bar' && row.label === 'Example car: Scheduled').data.map(point => point.y);
    })""")
    assert next(index for index, power in enumerate(bars[1]) if abs(power) > 0.1) < next(
        index for index, power in enumerate(bars[0]) if abs(power) > 0.1
    )
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    directory = Path("results/ci/flexible-references")
    directory.mkdir(parents=True, exist_ok=True)
    if page.viewport_size["width"] < 500:
        page.set_viewport_size({"width": 390, "height": 1600})
    for identifier in ("ems-costs", "reference-costs", "self-consumption-costs"):
        page.locator(f"figure:has(#{identifier})").screenshot(
            path=str(directory / f"{identifier}-{page.viewport_size['width']}.png")
        )
