"""Quarter-hour presentation preserves physical energy independently of exact accounting edges."""

from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[3] / "src/open_energy_planner/web/public"


@pytest.fixture
def quarter_page(browser):
    """Serve the actual balance module on a normal origin without backend dependencies."""
    with browser.new_context() as context:
        page = context.new_page()
        page.route(
            "http://quarters.test/balance.js",
            lambda route: route.fulfill(path=WEB / "balance.js", content_type="text/javascript"),
        )
        page.route(
            "http://quarters.test/",
            lambda route: route.fulfill(
                body="<html><body>Balance</body></html>", content_type="text/html"
            ),
        )
        page.goto("http://quarters.test/")
        yield page


def project(page, rows):
    """Read chart bins and prove the supplied intervals, prices and state points are unchanged."""
    return page.evaluate(
        """async rows => {
        const {quarterBalanceSeries} = await import('/balance.js');
        const series = [{type:'bar', name:'Import', intervals:rows},
            {name:'SoC', axis:'soc', points:[[0,50],[540000,45]]}];
        const original = JSON.stringify(series);
        const result = quarterBalanceSeries(series);
        return {result, unchanged: JSON.stringify(series) === original,
            sameState: result[1] === series[1]};
    }""",
        rows,
    )


def test_sunset_split_is_one_duration_weighted_quarter(quarter_page):
    """Nine minutes at 2 kW plus six at 5 kW contain .8 kWh, so one quarter averages 3.2 kW."""
    result = project(
        quarter_page,
        [
            {"start": 0, "end": 540000, "value": 2, "bonus": 0.04},
            {"start": 540000, "end": 900000, "value": 5, "bonus": 0},
        ],
    )
    assert result["unchanged"] and result["sameState"]
    bars = result["result"][0]["intervals"]
    assert len(bars) == 1
    assert (bars[0]["start"], bars[0]["end"]) == (0, 900000)
    assert bars[0]["value"] == pytest.approx(3.2)
    assert bars[0]["value"] * 0.25 == pytest.approx(0.8)


def test_coarse_hour_and_partial_horizon_keep_recorded_mean(quarter_page):
    """One recorded hour becomes four 4-kW bars. A following five-minute remainder remains explicitly partial."""
    result = project(
        quarter_page,
        [
            {"start": 0, "end": 3600000, "value": 4},
            {"start": 3600000, "end": 3900000, "value": 2},
        ],
    )
    bars = result["result"][0]["intervals"]
    assert len(bars) == 5
    assert [row["value"] for row in bars] == [4, 4, 4, 4, 2]
    assert all(row["end"] - row["start"] == 900000 for row in bars)
    assert bars[-1]["coveredEnd"] == 3900000
    assert sum(
        row["value"] * (row["coveredEnd"] - row["coveredStart"]) / 3600000 for row in bars
    ) == pytest.approx(4 + 1 / 6)


@pytest.mark.parametrize("fault", ["missing", "gap", "overlap"])
def test_incomplete_quarter_does_not_become_zero_or_partial_total(quarter_page, fault):
    """Unavailable, gapped or overlapping evidence invalidates only its quarter."""
    rows = [
        {"start": 0, "end": 540000, "value": 2},
        {"start": 540000, "end": 900000, "value": 5},
        {"start": 900000, "end": 1800000, "value": -3},
    ]
    if fault == "missing":
        rows[1]["value"] = None
    elif fault == "gap":
        rows[1]["start"] += 60000
    else:
        rows[1]["start"] -= 60000
    bars = project(quarter_page, rows)["result"][0]["intervals"]
    assert bars[0]["value"] is None
    assert bars[1]["value"] == -3


@pytest.mark.parametrize("offset_ms", [0, 3600000, -3600000])
def test_partial_opening_and_dst_instants_keep_absolute_quarters(quarter_page, offset_ms):
    """A five-minute opening fragment never invents ten minutes of energy, including repeated clock hours."""
    bars = project(
        quarter_page, [{"start": offset_ms + 600000, "end": offset_ms + 900000.0001, "value": 6}]
    )["result"][0]["intervals"]
    assert bars == [
        {
            "start": offset_ms,
            "end": offset_ms + 900000,
            "coveredStart": offset_ms + 600000,
            "coveredEnd": offset_ms + 900000,
            "value": 6,
        }
    ]
