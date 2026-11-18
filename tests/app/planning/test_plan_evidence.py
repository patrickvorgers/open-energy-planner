"""Original plan evidence is factual, immutable, interval-aligned and independent of solver execution."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.savings.evidence import bonus_cards, evidence, interval_evidence
from tests.app.savings.support.scenarios import START, problem


def original_plan():
    """1.5 kWh PV supplies .5 kWh house plus 1 kWh charging in a quarter: 4000 W, stored 2 to 3 kWh."""
    physics = problem(pv_kwh=1.5, load_kwh=0.5)
    return {
        "plan_id": "evidence-original",
        "strategy": "minimize cost",
        "solution_status": "feasible",
        "currency": "EUR",
        "comparison_names": {"battery": "House battery", "roof": "South roof"},
        "input_snapshot": {
            "contract_version": "original-contract",
            "problem": json.loads(
                json.dumps(asdict(physics), default=lambda value: value.isoformat())
            ),
            "tariff_evidence": [
                {
                    "start": START.isoformat(),
                    "end": (START + timedelta(minutes=15)).isoformat(),
                    "price_kind": "published",
                }
            ],
        },
        "expected_flows": [
            {
                "grid_import": 0,
                "grid_export": 0,
                "batteries": [
                    {"device_id": "battery", "charge_kwh": 1, "discharge_kwh": 0, "end_kwh": 3}
                ],
                "inverters": [
                    {"inverter_id": "inv", "import_kwh": 0, "export_kwh": 0.5, "clipping": False}
                ],
                "solar": [{"source_id": "roof", "used_kwh": 1.5, "enabled": True}],
            }
        ],
    }


def facts(cards):
    """Index fact rows for precise numerical assertions, independently of prose/formatting order."""
    return {row["label"]: row["value"] for item in cards for row in item["facts"]}


def test_evidence_preserves_original_inputs_and_never_claims_causation():
    """A 4000-W charge limit is reached. .3/- .1 original rates and 75% end SoC stay factual, not a reason to charge."""
    plan = original_plan()
    before = deepcopy(plan)
    result = evidence(plan)
    summary = facts(result["summary"])
    interval = facts(result["intervals"][0]["cards"])
    assert plan == before
    assert result["plan_id"] == "evidence-original"
    assert "Original contract version" not in summary
    assert plan["input_snapshot"]["contract_version"] == "original-contract"
    assert summary["Solution"] == "Feasible: Financial optimality is not proved."
    assert summary["Price basis"] == "Estimated from a repeated trailing price window."
    assert interval["Import rate before conditional bonuses"] == 0.3
    assert interval["Export rate before conditional bonuses"] == -0.1
    assert interval["End SoC"] == 75
    assert interval["Market price evidence"] == "Published"
    notes = [note for item in result["intervals"][0]["cards"] for note in item["observations"]]
    assert notes == ["Charge power limit reached in this interval."]
    assert "because" not in json.dumps(result).lower()
    assert "pv_to_battery" not in json.dumps(result)


@pytest.mark.parametrize("offset_minutes", [0, 120])
def test_price_evidence_matches_instants_instead_of_timestamp_spelling(offset_minutes):
    """Equivalent UTC and +02:00 timestamps identify the same published quarter, not a missing-price fallback."""
    from datetime import timezone

    plan = original_plan()
    zone = timezone(timedelta(minutes=offset_minutes))
    plan["input_snapshot"]["tariff_evidence"][0].update(
        start=START.astimezone(zone).isoformat(),
        end=(START + timedelta(minutes=15)).astimezone(zone).isoformat(),
    )
    assert facts(evidence(plan)["intervals"][0]["cards"])["Market price evidence"] == "Published"


@pytest.mark.parametrize(
    "model,label",
    [
        ("startup-v1", "Start profile"),
        ("weekday-hour-v1", "Learned weekday/hour"),
        ("ridge-v1", "ML regression"),
        ("startup-v2", "Start profile"),
        ("weekday-hour-v2", "Learned weekday/hour"),
        ("ridge-v2", "ML regression"),
    ],
)
def test_active_household_model_is_visible_per_interval_and_whole_plan(model, label):
    """The frozen active start/profile/ML model labels the household forecast. A newer candidate cannot be substituted."""
    plan = original_plan()
    plan["input_snapshot"]["demand_evidence"] = [
        {
            "model": model,
            "quality": "estimated",
            "forecast_version": "original-load-forecast",
            "excluded_devices": [],
        }
    ]
    result = evidence(plan)
    whole = next(
        item for item in result["summary"] if item["title"] == "Household forecast: Whole period"
    )
    selected = next(
        item
        for item in result["intervals"][0]["cards"]
        if item["title"] == "Household forecast: This interval"
    )
    for item in (whole, selected):
        assert label in facts([item])["Forecast model"]
        assert "Forecast version" not in facts([item])
    assert (
        plan["input_snapshot"]["demand_evidence"][0]["forecast_version"] == "original-load-forecast"
    )


def test_solar_forecast_provenance_is_matched_per_source():
    """Roof and garage retain their own model records. The ML-corrected garage must not relabel the physical roof model."""
    plan = original_plan()
    physics = plan["input_snapshot"]["problem"]
    physics["pv_sources"].append(
        {"source_id": "garage", "inverter_id": "inv", "can_curtail": False}
    )
    physics["intervals"][0]["pv_sources"] = [["roof", 0.5], ["garage", 1]]
    plan["input_snapshot"]["solar_evidence"] = [
        {
            "model": "pvwatts-v2",
            "quality": "provisional",
            "source_ids": ["roof"],
            "forecast_version": "physical-roof",
        },
        {
            "model": "pvwatts-calibrated-v2",
            "quality": "shadow",
            "source_ids": ["garage"],
            "forecast_version": "recorded-garage",
        },
    ]
    result = evidence(plan)
    roof = next(
        item for item in result["summary"] if item["title"] == "South roof: Whole-period forecast"
    )
    garage = next(
        item for item in result["summary"] if item["title"] == "Solar 2: Whole-period forecast"
    )
    assert facts([roof])["Forecast model"] == "Physical solar model (PVWatts quarter geometry)"
    assert "learned corrections" in facts([garage])["Forecast model"]
    assert facts([roof])["Available forecast: DC: Whole period"] == 0.5
    assert facts([garage])["Available forecast: DC: Whole period"] == 1


def test_whole_horizon_totals_are_distinct_from_selected_quarter():
    """Two quarters total 1 kWh demand, 3 kWh PV and 2 kWh battery charge. Each quarter still shows .5/1.5/1 kWh."""
    plan = original_plan()
    second = deepcopy(plan["input_snapshot"]["problem"]["intervals"][0])
    second["start"] = (START + timedelta(minutes=15)).isoformat()
    plan["input_snapshot"]["problem"]["intervals"].append(second)
    flow = deepcopy(plan["expected_flows"][0])
    flow["batteries"][0]["end_kwh"] = 4
    plan["expected_flows"].append(flow)
    result = evidence(plan)
    totals = facts(result["summary"])
    assert result["period"]["duration_h"] == 0.5
    assert totals["Household demand forecast: AC"] == 1
    assert totals["Available solar forecast: DC"] == 3
    assert totals["Total charge: DC terminal"] == 2
    assert totals["Final SoC"] == 100
    assert facts(result["intervals"][0]["cards"])["Household demand: AC: This interval"] == 0.5


def test_missing_flows_keep_interval_indices_and_never_become_zero():
    """A missing second-quarter outcome stays absent while its forecast remains available at the correct time."""
    plan = original_plan()
    second = deepcopy(plan["input_snapshot"]["problem"]["intervals"][0])
    second["start"] = (START + timedelta(minutes=15)).isoformat()
    plan["input_snapshot"]["problem"]["intervals"].append(second)
    result = evidence(plan)
    assert len(result["intervals"]) == 2
    assert result["intervals"][1]["start"] == second["start"]
    assert result["intervals"][1]["issue"]
    assert "Import" not in facts(result["intervals"][1]["cards"])
    assert facts(result["intervals"][1]["cards"])["Household demand: AC: This interval"] == 0.5


@pytest.mark.parametrize(
    "collection,key,label",
    [
        ("batteries", "charge_kwh", "Charge: DC terminal"),
        ("batteries", "end_kwh", "End SoC"),
        ("inverters", "import_kwh", "AC import"),
        ("solar", "used_kwh", "Planned use: DC"),
    ],
)
def test_partial_device_records_remain_readable(collection, key, label):
    """A missing individual numeric outcome is explicitly unrecorded. The other original facts remain readable."""
    plan = original_plan()
    plan["expected_flows"][0][collection][0].pop(key)
    result = evidence(plan)["intervals"][0]
    assert result["issue"]
    assert facts(result["cards"])[label] is None
    assert facts(result["cards"])["Household demand: AC: This interval"] == 0.5


@pytest.mark.parametrize("strategy", ["minimize cost", "minimize consumption"])
def test_no_storage_or_solar_does_not_invent_devices(strategy):
    """A grid-only snapshot retains its chosen objective and contains no fictional battery/solar cards."""
    plan = original_plan()
    physics = problem()
    physics = replace(
        physics,
        batteries=(),
        pv_sources=(),
        terminal_valuations=(),
        intervals=(replace(physics.intervals[0], pv_sources=()),),
    )
    plan["input_snapshot"]["problem"] = json.loads(
        json.dumps(asdict(physics), default=lambda value: value.isoformat())
    )
    plan["strategy"] = strategy
    plan["expected_flows"][0].update(batteries=[], solar=[])
    result = evidence(plan)
    assert facts(result["summary"])["Strategy"].lower().startswith(strategy)
    assert not any(
        "battery" in item["title"] or "solar" in item["title"]
        for item in result["intervals"][0]["cards"]
    )


def test_bonus_caps_and_missing_credits_are_separate_from_base_export_price():
    """The archived .1 bonus and 99/100-kWh counter are visible. Absent credit evidence is None, not zero or assumed earnings."""
    plan = original_plan()
    plan["input_snapshot"]["problem"]["export_bonuses"] = [
        {
            "rule_id": "solar-bonus",
            "rates": [0.1],
            "caps_kwh": [100],
            "periods": ["year"],
            "initial_kwh": 99,
        }
    ]
    result = evidence(plan)
    rows = facts(result["intervals"][0]["cards"])
    assert rows["Effective bonus rate"] == 0.1
    assert rows["Period cap"] == 100
    assert rows["Counter before this interval"] == 99
    assert rows["Planned credited volume"] is None
    assert rows["Export rate before conditional bonuses"] == -0.1


@pytest.mark.parametrize("end_kwh,expected", [(0, "reserve"), (4, "usable capacity")])
def test_storage_boundary_observations_are_not_price_explanations(end_kwh, expected):
    """Ending exactly at reserve/capacity is reported literally, without claiming that a price caused that outcome."""
    plan = original_plan()
    plan["expected_flows"][0]["batteries"][0]["end_kwh"] = end_kwh
    notes = [
        note for item in evidence(plan)["intervals"][0]["cards"] for note in item["observations"]
    ]
    assert any(expected in note for note in notes)
    assert not any("price" in note or "because" in note for note in notes)


def test_disabled_solar_and_missing_device_outcomes_remain_explicit():
    """A disabled PV command is factual. A missing battery outcome flags incomplete evidence rather than an idle battery."""
    plan = original_plan()
    plan["input_snapshot"]["problem"]["pv_sources"][0]["can_curtail"] = True
    plan["expected_flows"][0]["solar"][0].update(enabled=False, used_kwh=0)
    plan["expected_flows"][0]["batteries"] = []
    result = evidence(plan)["intervals"][0]
    assert result["issue"]
    assert "End SoC" not in facts(result["cards"])
    assert facts(result["cards"])["Planned use: DC"] == 0
    assert any(
        "Production disabled in this plan." in item["observations"] for item in result["cards"]
    )


def test_nearby_values_do_not_claim_a_binding_power_rating():
    """3996 W is below the 4000-W rating: a nearly full charging bar is not evidence that the limit was reached."""
    plan = original_plan()
    plan["expected_flows"][0]["batteries"][0]["charge_kwh"] = 0.999
    notes = [
        note for item in evidence(plan)["intervals"][0]["cards"] for note in item["observations"]
    ]
    assert notes == []


def test_grid_following_has_no_fictional_fixed_power_target():
    """An archived grid-following command is shown as local regulation with an inapplicable fixed target, never zero W."""
    plan = original_plan()
    plan["steps"] = [
        {
            "start": START.isoformat(),
            "end": (START + timedelta(minutes=15)).isoformat(),
            "devices": {"battery": {"mode": "follow_grid_target", "power_w": None}},
        }
    ]
    rows = facts(evidence(plan)["intervals"][0]["cards"])
    assert rows["Control mode"] == "Zero on the meter: Local regulation is active."
    assert rows["Fixed power target (+ discharge)"] == "Not applicable"


def test_bonus_period_reset_uses_its_own_archived_counter():
    """A bonus starts at 99, reaches 100 and resets in the next period. The next interval gets zero, not yesterday's 100."""
    plan = original_plan()
    second = deepcopy(plan["input_snapshot"]["problem"]["intervals"][0])
    second["start"] = (START + timedelta(minutes=15)).isoformat()
    plan["input_snapshot"]["problem"]["intervals"].append(second)
    plan["input_snapshot"]["problem"]["export_bonuses"] = [
        {
            "rule_id": "bonus",
            "rates": [0.1, 0.2],
            "caps_kwh": [100, 100],
            "periods": ["old", "new"],
            "initial_kwh": 99,
        }
    ]
    plan["expected_bonuses"] = [
        {"rule_id": "bonus", "quota_kwh": [1, 0.5], "counters_kwh": [100, 0.5]}
    ]
    result = evidence(plan)
    assert facts(result["intervals"][0]["cards"])["Counter before this interval"] == 99
    assert facts(result["intervals"][1]["cards"])["Counter before this interval"] == 0
    assert facts(result["intervals"][1]["cards"])["Planned credited volume"] == 0.5


def test_legacy_plan_reports_missing_evidence():
    """A legacy plan without installation input remains readable with explicit unavailable evidence."""
    assert evidence({"plan_id": "legacy"})["issue"]


def test_missing_currency_does_not_hide_other_original_evidence():
    """A plan saved without a contract currency keeps its physical facts and marks the currency unrecorded."""
    plan = original_plan()
    plan["currency"] = None
    result = evidence(plan)
    assert "issue" not in result
    assert facts(result["summary"])["Currency"] is None
    assert facts(result["intervals"][0]["cards"])["End SoC"] == 75


def test_errors_after_physical_context_return_an_explicit_evidence_issue():
    """Malformed archived tariff timing cannot turn a readable original plan into a failed comparison response."""
    plan = original_plan()
    plan["input_snapshot"]["tariff_evidence"][0]["start"] = "not a timestamp"
    before = deepcopy(plan)
    result = evidence(plan)
    assert result["issue"]
    assert result["summary"] == []
    assert result["intervals"] == []
    assert plan == before


def test_evidence_api_is_authenticated_and_does_not_change_or_resolve_the_plan(tmp_path):
    """Repeated reads return the original plan facts without issuing a new plan, changing strategy or overwriting archived inputs."""
    from tests.frontend.browser.savings_fixture import seed

    seed(tmp_path)

    async def scenario():
        """Exercise the real archive/read API and compare the original document before and after evidence reads."""
        token = "plan-evidence-test-0123456789"
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            path = "/v1/plans/browser-original-plan/comparison"
            assert (await client.get(path)).status == 401
            headers = {"Authorization": "Bearer " + token}
            first = await (await client.get(path, headers=headers)).json()
            second = await (await client.get(path, headers=headers)).json()
            assert first["plan"] == second["plan"]
            assert first["evidence"] == second["evidence"]
            assert first["evidence"]["plan_id"] == first["plan"]["plan_id"]
            assert first["evidence"]["intervals"]

    asyncio.run(scenario())


def test_invalid_archived_forecast_quality_is_reported_without_breaking_plan_reads():
    """A null forecast quality is malformed evidence, not an AttributeError that fails the comparison API."""
    plan = original_plan()
    plan["input_snapshot"]["demand_evidence"] = [{"quality": None}]
    result = evidence(plan)
    assert result["issue"]
    assert result["summary"] == []


def test_interval_devices_group_by_original_inverter_without_changing_evidence():
    """Roof and battery share inverter one, while a separate garage array stays on inverter two."""
    plan = original_plan()
    physics = plan["input_snapshot"]["problem"]
    physics["inverters"].append({**physics["inverters"][0], "inverter_id": "garage-inverter"})
    physics["pv_sources"].append(
        {"source_id": "garage", "inverter_id": "garage-inverter", "can_curtail": False}
    )
    physics["intervals"][0]["pv_sources"].append(["garage", 0])
    plan["expected_flows"][0]["inverters"].append(
        {"inverter_id": "garage-inverter", "import_kwh": 0, "export_kwh": 0, "clipping": False}
    )
    plan["expected_flows"][0]["solar"].append(
        {"source_id": "garage", "used_kwh": 0, "enabled": True}
    )
    before = deepcopy(plan)
    result = evidence(plan)
    assert result.get("issue") is None
    cards = result["intervals"][0]["cards"]
    groups = {item["title"]: item.get("group") for item in cards}
    assert groups["House battery: Planned battery"] == {"id": "inv", "title": "Inverter 1"}
    assert groups["South roof: Solar"] == {"id": "inv", "title": "Inverter 1"}
    assert groups["Solar 2: Solar"] == {"id": "garage-inverter", "title": "Inverter 2"}
    connections = [item for item in cards if item["title"] == "Connection and limits"]
    by_inverter = {item["group"]["id"]: facts([item]) for item in connections}
    assert by_inverter["inv"]["AC export"] == 0.5
    assert by_inverter["garage-inverter"]["AC export"] == 0
    assert plan == before


def test_bonus_title_omits_technical_identity_and_preserves_original_rule():
    """A readable bonus title retains its independently specified rate, quota and archived identity."""
    plan = original_plan()
    plan["input_snapshot"]["problem"]["export_bonuses"] = [
        {
            "rule_id": "example-bonus-rule",
            "initial_kwh": 2,
            "rates": [0.07],
            "caps_kwh": [100],
            "periods": ["example-period"],
        }
    ]
    before = deepcopy(plan)
    cards = bonus_cards(plan, 0)
    assert [item["title"] for item in cards] == ["Export bonus"]
    assert facts(cards)["Effective bonus rate"] == 0.07
    assert facts(cards)["Counter before this interval"] == 2
    assert plan == before


def test_multiple_bonus_titles_distinguish_separate_counters_without_rule_ids():
    """Equal-rate bonus rules retain separate original counters under stable numbered labels."""
    plan = original_plan()
    plan["input_snapshot"]["problem"]["export_bonuses"] = [
        {
            "rule_id": identity,
            "initial_kwh": stock,
            "rates": [0.07],
            "caps_kwh": [100],
            "periods": ["example-period"],
        }
        for identity, stock in (("first-example", 2), ("second-example", 7))
    ]
    before = deepcopy(plan)
    cards = bonus_cards(plan, 0)
    assert [item["title"] for item in cards] == ["Export bonus 1", "Export bonus 2"]
    assert [facts([item])["Counter before this interval"] for item in cards] == [2, 7]
    assert plan == before


def test_solar_command_survives_absent_physical_outcomes():
    """An archived curtailment command remains readable without inventing missing solar use."""
    plan = original_plan()
    plan["input_snapshot"]["problem"]["pv_sources"][0]["can_curtail"] = True
    plan["expected_flows"] = []
    plan["steps"] = [
        {
            "start": START.isoformat(),
            "end": (START + timedelta(minutes=15)).isoformat(),
            "devices": {"roof": {"enabled": False}},
        }
    ]
    before = deepcopy(plan)
    interval = evidence(plan)["intervals"][0]
    solar = next(item for item in interval["cards"] if item["title"] == "South roof: Solar")
    assert facts([solar])["Solar production"] == "Curtailment requested"
    assert facts([solar])["Curtailment permitted"] == "Yes"
    assert facts([solar])["Planned use: DC"] is None
    assert solar["group"] == {"id": "inv", "title": "Inverter 1"}
    assert interval["issue"]
    assert plan == before


def test_household_label_excludes_service_energy_added_to_physical_context():
    """The archived 0.5-kWh house remains separate from 0.2 kWh of additional service demand."""
    plan = original_plan()
    physical = problem(pv_kwh=1.5, load_kwh=0.7)
    before = deepcopy(plan)
    rows = facts(interval_evidence(plan, physical, 0)["cards"])
    assert rows["Household demand: AC: This interval"] == 0.5
    assert plan == before


@pytest.mark.parametrize(
    ("grid", "expected"),
    [
        ({"grid_to_load": 0.25, "grid_to_battery": 0.1}, 0.35),
        ({"grid_import": 0, "grid_to_load": 0.25, "grid_to_battery": 0.1}, 0),
        ({"grid_to_load": 0.25}, None),
        ({}, None),
    ],
)
def test_legacy_grid_components_preserve_known_zero_and_missing_totals(grid, expected):
    """Complete legacy components yield 0.35 kWh, while an explicit zero wins and missing parts stay unknown."""
    plan = original_plan()
    flow = plan["expected_flows"][0]
    flow.pop("grid_import")
    flow.pop("grid_export")
    flow.update(grid, pv_to_grid=0.12, battery_to_grid=0.03)
    before = deepcopy(plan)
    result = evidence(plan)
    interval = facts(result["intervals"][0]["cards"])
    summary = facts(result["summary"])
    assert interval["Import"] == expected
    assert summary["Total import"] == expected
    assert interval["Export"] == 0.15
    assert summary["Total export"] == 0.15
    assert bool(result["intervals"][0]["issue"]) == (expected is None)
    assert plan == before


def test_duplicate_battery_names_retain_archived_order_labels_and_independent_commands():
    """Same-named batteries keep first fixed-power and second meter-following cards without renaming the archive."""
    plan = original_plan()
    physics = plan["input_snapshot"]["problem"]
    second = deepcopy(physics["batteries"][0])
    second["device_id"] = "spare"
    second["can_follow_grid"] = True
    physics["batteries"].append(second)
    physics["terminal_valuations"].append(
        {**physics["terminal_valuations"][0], "device_id": "spare"}
    )
    plan["comparison_names"]["spare"] = "House battery"
    plan["expected_flows"][0]["batteries"].append(
        {"device_id": "spare", "charge_kwh": 0, "discharge_kwh": 0, "end_kwh": 2}
    )
    plan["steps"] = [
        {
            "start": START.isoformat(),
            "end": (START + timedelta(minutes=15)).isoformat(),
            "devices": {
                "battery": {"mode": "fixed_power", "power_w": -4000},
                "spare": {"mode": "follow_grid_target", "power_w": None},
            },
        }
    ]
    before = deepcopy(plan)
    cards = evidence(plan)["intervals"][0]["cards"]
    first_card = next(
        item for item in cards if item["title"] == "House battery: 1: Planned battery"
    )
    second_card = next(
        item for item in cards if item["title"] == "House battery: 2: Planned battery"
    )
    assert facts([first_card])["Control mode"] == "Fixed power"
    assert facts([second_card])["Control mode"] == "Zero on the meter: Local regulation is active."
    assert plan == before
