"""Independent AC cash arithmetic needs neither forecast dispatch nor historical battery stock."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from open_energy_planner.contracts.bonus import CounterSeed
from open_energy_planner.contracts.calculation.condition import (
    ComparisonCondition,
    ComparisonOperator,
)
from open_energy_planner.contracts.calculation.formula import Formula
from open_energy_planner.contracts.export_meter import export_identity, seed_reading
from open_energy_planner.savings.accounting import bill, bonus_payment, measured_cash_flows
from tests.app.savings.support.scenarios import START, bindings, contract, frame, prices


@pytest.mark.parametrize(("market", "uncapped", "capped"), [(0.2, 0.4, 0.1), (-0.2, 0, 0)])
def test_savings_ignores_only_the_volume_cap_at_real_bonus_edges(market, uncapped, capped):
    """Five of fifteen minutes qualify at twice the positive market price, with a quarter-kWh cap."""
    schedule = contract(bonus=True)
    rule = replace(
        schedule.bonuses[0],
        cap_kwh=0.25,
        window="clock",
        start_time="10:00",
        end_time="10:05",
        rate_formula=Formula("2 * market"),
        price_condition=ComparisonCondition(
            Formula("market"), ComparisonOperator.GREATER_THAN, Decimal(0)
        ),
    )
    schedule = replace(schedule, bonuses=(rule,))
    published = [{**prices()[0], "price": market}]
    report = bill(schedule, frame(), 0, 3, None, (), published, None, uncapped=True)
    assert report["bonus"] == pytest.approx(uncapped)
    assert len(report["tariffs"]) == 2
    usage = {rule.rule_id + "|" + rule.period_start(START).isoformat(): 0.0}
    original = bill(schedule, frame(), 0, 3, usage, (), published, None)
    assert original["bonus"] == pytest.approx(capped)


def test_capped_billing_uses_all_confirmed_export_meter_members():
    """The recorded usage leaves .70 kWh under the 2-kWh cap, paying .07 on the next kWh."""
    schedule = contract(bonus=True)
    sources = ("sensor.export_a", "sensor.export_b")
    seed = CounterSeed(
        "bonus",
        schedule.bonuses[0].period_start(START),
        1,
        300,
        export_identity(sources),
        START,
        ((sources[0], 100), (sources[1], 200)),
    )
    counters = {}
    paid = bonus_payment(
        schedule,
        START,
        START + timedelta(minutes=15),
        0.1,
        1,
        counters,
        (seed,),
        {sources[0]: 100.2, sources[1]: 200.1, "sensor.import": 99999},
        None,
    )
    assert paid == pytest.approx(0.07)
    assert list(counters.values()) == pytest.approx([2.3])
    with pytest.raises(ValueError, match="reset"):
        seed_reading(seed, {sources[0]: 99, sources[1]: 202})


def test_discharge_reconstructs_without_battery_from_measured_flows_without_soc():
    """One imported plus two discharged kWh implies three without battery, saving .60 at .30."""
    measured = replace(frame(discharged_kwh=2), battery_start={}, battery_end={})
    flows = measured_cash_flows(measured, bindings())
    assert flows == {"actual": (1, 0), "without_battery": (3, 0)}
    actual = bill(contract(), measured, *flows["actual"], None, (), prices(), None, uncapped=True)
    without = bill(
        contract(), measured, *flows["without_battery"], None, (), prices(), None, uncapped=True
    )
    assert actual["grid_cash"] == pytest.approx(0.30)
    assert without["grid_cash"] - actual["grid_cash"] == pytest.approx(0.60)


def test_charging_counts_when_bought_and_can_make_a_negative_daily_saving():
    """Three imported kWh include two for charging. Without battery costs .30 rather than .90."""
    measured = frame(imported_kwh=3)
    measured = replace(measured, channels={**measured.channels, "sensor.charge": 2})
    selected = bindings()
    selected["terms"].append({"entity_id": "sensor.charge", "sign": -1})
    flows = measured_cash_flows(measured, selected)
    assert flows["without_battery"] == (1, 0)
    actual = bill(contract(), measured, *flows["actual"], None, (), prices(), None, uncapped=True)
    without = bill(
        contract(), measured, *flows["without_battery"], None, (), prices(), None, uncapped=True
    )
    assert without["grid_cash"] - actual["grid_cash"] == pytest.approx(-0.60)


def test_no_battery_contribution_keeps_simultaneous_recorded_gross_grid_flows():
    """A quarter may import and export. With no battery contribution its saving must remain zero."""
    flows = measured_cash_flows(frame(imported_kwh=1, exported_kwh=0.5), bindings())
    assert flows["actual"] == flows["without_battery"] == (1, 0.5)


def test_discharge_first_replaces_export_before_increasing_without_battery_import():
    """Two exported kWh with one discharged become one exported kWh without the battery."""
    flows = measured_cash_flows(
        frame(imported_kwh=0, exported_kwh=2, pv_kwh=2, discharged_kwh=1), bindings()
    )
    assert flows["without_battery"] == (0, 1)


def test_missing_meter_is_not_substituted_by_zero():
    """An absent discharge counter cannot establish the no-battery flow even with complete grid meters."""
    measured = frame()
    measured = replace(
        measured,
        channels={
            key: value for key, value in measured.channels.items() if key != "sensor.discharge"
        },
    )
    with pytest.raises(KeyError):
        measured_cash_flows(measured, bindings())


@pytest.mark.parametrize("price, expected", [("-0.30", -0.60), ("0", 0)])
def test_signed_and_zero_prices_keep_independent_cash_oracles(price, expected):
    """Two discharged kWh avoid buying at the signed rate. Negative buying prices reverse the saving."""
    measured = frame(discharged_kwh=2)
    flows = measured_cash_flows(measured, bindings())
    actual = bill(
        contract(buy=price), measured, *flows["actual"], None, (), prices(), None, uncapped=True
    )
    without = bill(
        contract(buy=price),
        measured,
        *flows["without_battery"],
        None,
        (),
        prices(),
        None,
        uncapped=True,
    )
    assert without["grid_cash"] - actual["grid_cash"] == pytest.approx(expected)


def test_dc_solar_meter_cannot_be_billed_as_measured_ac_generation():
    """A DC production counter cannot establish the AC alternative behind a hybrid converter."""
    from open_energy_planner.savings.model import PendingSettlement

    selected = bindings()
    selected["solar"][0]["measurement_boundary"] = "dc"
    with pytest.raises(PendingSettlement, match="requires measured AC solar"):
        measured_cash_flows(frame(), selected)
