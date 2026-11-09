"""Independent AC/DC conservation examples render thermal consumers through the actual Chart.js engine."""

from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import pytest

WEB = Path(__file__).resolve().parents[3] / "src/open_energy_planner/web/public"


def joint_plan():
    """Supply 1 solar + .5 battery DC, losing .3 in conversion, plus .1 grid AC for 1.3 AC demand.

    Demand is .4 household + .2 EV + .1 appliance + .4 first boiler + .2 second boiler.
    A quarter hour converts these independent energy quantities to four times as many kW.
    """
    return {
        "input_snapshot": {
            "problem": {
                "intervals": [
                    {"start": "2026-10-27T12:00:00Z", "duration_h": 0.25, "load_kwh": 0.4}
                ],
                "batteries": [
                    {
                        "device_id": "battery",
                        "inverter_id": "hybrid",
                        "dc_efficiency": 1,
                        "battery": {"capacity_kwh": 10, "initial_kwh": 5},
                    }
                ],
                "inverters": [{"inverter_id": "hybrid", "efficiency": 0.8}],
                "pv_sources": [{"source_id": "roof", "inverter_id": "hybrid"}],
                "ev_work": [
                    {
                        "session_id": "ev-session",
                        "vehicle_id": "car",
                        "capacity_kwh": 20,
                        "initial_soc": 50,
                    }
                ],
                "thermal_work": [{"device_id": "boiler-a"}, {"device_id": "boiler-b"}],
            }
        },
        "appliance_schedule": [{"interval_kwh": [0.1]}],
        "expected_flows": [
            {
                "grid_import": 0.1,
                "grid_export": 0,
                "solar": [{"source_id": "roof", "used_kwh": 1}],
                "batteries": [
                    {"device_id": "battery", "charge_kwh": 0, "discharge_kwh": 0.5, "end_kwh": 4.5}
                ],
                "inverters": [{"inverter_id": "hybrid", "import_kwh": 0, "export_kwh": 1.2}],
                "ev": [{"session_id": "ev-session", "ac_kwh": 0.2, "stored_kwh": 0.18}],
                "thermal": [
                    {"device_id": "boiler-a", "ac_kwh": 0.4},
                    {"device_id": "boiler-b", "ac_kwh": 0.2},
                ],
            }
        ],
    }


@pytest.fixture
def balance_page(browser):
    """Load the actual balance module and bundled Chart.js without application API dependencies."""
    with browser.new_context(viewport={"width": 1280, "height": 900}) as context:
        page = context.new_page()

        def serve(route):
            """Serve only the presentation module and a chart surface on a normal HTTP origin."""
            path = urlsplit(route.request.url).path
            if path == "/balance.js":
                route.fulfill(path=WEB / "balance.js", content_type="text/javascript")
            else:
                route.fulfill(
                    body='<canvas id="balance" width="1000" height="500"></canvas>',
                    content_type="text/html",
                )

        page.route("http://balance.test/**", serve)
        page.goto("http://balance.test/")
        page.add_script_tag(path=str(WEB / "chart.umd.min.js"))
        yield page


def render(page, plan):
    """Render all signed installation bars and return their real Chart.js values plus converter evidence."""
    return page.evaluate(
        """async plan => {
        const {energyBalances} = await import('/balance.js');
        const balances = energyBalances(plan, new Map([['boiler-a', 'Kitchen boiler'], ['boiler-b', 'Shower tank']]));
        const previous = Chart.getChart(document.querySelector('canvas'));
        if (previous) previous.destroy();
        const chart = new Chart(document.querySelector('canvas'), {
            type: 'bar', data: {datasets: balances[0].series.filter(series => series.type === 'bar').map(series => ({
                label: series.name, backgroundColor: series.color, stack: series.stack,
                data: series.intervals.map(interval => ({x: interval.start, y: interval.value}))
            }))}, options: {responsive:false, animation:false, parsing:false, scales:{x:{type:'linear'}, y:{stacked:true}}}
        });
        return {balances, bars: chart.data.datasets.map(dataset => ({name:dataset.label, color:dataset.backgroundColor, values:dataset.data.map(point => point.y)}))};
    }""",
        plan,
    )


def test_named_thermal_consumers_conserve_joint_installation_without_changing_converter(
    balance_page,
):
    """Two named thermal bars consume 1.6 and .8 kW while supply and consumption both total 6.4 kW."""
    result = render(balance_page, joint_plan())
    plant, converter = result["balances"]
    assert plant["missing"] == converter["missing"] == 0
    values = {row["name"]: row["values"][0] for row in result["bars"]}
    assert values["Kitchen boiler: Scheduled"] == pytest.approx(-1.6)
    assert values["Shower tank: Scheduled"] == pytest.approx(-0.8)
    assert values["Appliance 1: Scheduled"] == pytest.approx(-0.4)
    assert values["Charge point 1: Scheduled"] == pytest.approx(-0.8)
    assert sum(value for value in values.values() if value > 0) == pytest.approx(6.4)
    assert sum(value for value in values.values() if value < 0) == pytest.approx(-6.4)
    assert sum(values.values()) == pytest.approx(0)
    heater_colors = {
        row["color"]
        for row in result["bars"]
        if row["name"].startswith(("Kitchen boiler:", "Shower tank:"))
    }
    assert len(heater_colors) == 2
    assert all(
        not row["name"].startswith(("Kitchen boiler:", "Shower tank:"))
        for row in converter["series"]
    )
    converter_values = [
        row["intervals"][0]["value"] for row in converter["series"] if row.get("type") == "bar"
    ]
    assert sum(converter_values) == pytest.approx(0)


def test_reference_without_battery_keeps_identical_fixed_thermal_schedule(balance_page):
    """Removing .5 battery DC increases grid import by .4 AC, leaving both boiler bars unchanged."""
    original = render(balance_page, joint_plan())
    reference = joint_plan()
    reference["input_snapshot"]["problem"]["batteries"] = []
    reference["expected_flows"][0]["batteries"] = []
    reference["expected_flows"][0]["grid_import"] = 0.5
    reference["expected_flows"][0]["inverters"][0]["export_kwh"] = 0.8
    result = render(balance_page, reference)
    assert result["balances"][0]["missing"] == 0
    for result_document in (original, result):
        thermal = {
            row["name"]: row["values"]
            for row in result_document["bars"]
            if row["name"].startswith(("Kitchen boiler:", "Shower tank:"))
        }
        assert thermal == {"Kitchen boiler: Scheduled": [-1.6], "Shower tank: Scheduled": [-0.8]}
    assert sum(row["values"][0] for row in result["bars"]) == pytest.approx(0)


@pytest.mark.parametrize(
    "fault",
    ["absent", "partial", "duplicate", "wrong_identity", "negative", "null_row", "duplicate_work"],
)
def test_missing_or_inconsistent_thermal_evidence_is_a_gap_only_at_installation(
    balance_page, fault
):
    """The chart cannot invent thermal consumption to close a residual. Converter physics remains independent."""
    plan = joint_plan()
    flow = plan["expected_flows"][0]
    if fault == "absent":
        flow.pop("thermal")
    elif fault == "partial":
        flow["thermal"].pop()
    elif fault == "duplicate":
        flow["thermal"][1] = deepcopy(flow["thermal"][0])
    elif fault == "wrong_identity":
        flow["thermal"][1]["device_id"] = "unconfigured"
    elif fault == "null_row":
        flow["thermal"][0] = None
    elif fault == "duplicate_work":
        plan["input_snapshot"]["problem"]["thermal_work"][1] = {"device_id": "boiler-a"}
    else:
        flow["thermal"][0]["ac_kwh"] = -0.4
    result = render(balance_page, plan)
    assert result["balances"][0]["missing"] == 1
    assert all(row["values"] == [None] for row in result["bars"])
    assert result["balances"][1]["missing"] == 0


def test_nonthermal_plan_has_identical_existing_consumption_series(balance_page):
    """A nonthermal plan still exports .5 AC after .7 AC demand, without introducing zero boiler bars."""
    plan = joint_plan()
    plan["input_snapshot"]["problem"].pop("thermal_work")
    flow = plan["expected_flows"][0]
    flow.pop("thermal")
    flow.update(grid_import=0, grid_export=0.5)
    result = render(balance_page, plan)
    assert result["balances"][0]["missing"] == 0
    values = {row["name"]: row["values"][0] for row in result["bars"]}
    assert not any(name.startswith(("Kitchen boiler:", "Shower tank:")) for name in values)
    assert values["Household demand: AC"] == pytest.approx(-1.6)
    assert values["Appliance 1: Scheduled"] == pytest.approx(-0.4)
    assert values["Charge point 1: Scheduled"] == pytest.approx(-0.8)
    assert values["Grid export: AC"] == pytest.approx(-2)
    assert sum(values.values()) == pytest.approx(0)
