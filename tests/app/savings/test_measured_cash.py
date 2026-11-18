"""Independent AC cash arithmetic needs neither forecast dispatch nor historical battery stock."""

from dataclasses import replace

import pytest

from open_energy_planner.savings.accounting import cash_account, measured_cash_flows
from tests.app.savings.support.scenarios import bindings, contract, frame, prices


def test_discharge_reconstructs_without_battery_from_measured_flows_without_soc():
    """One imported plus two discharged kWh implies three without battery, saving .60 at .30."""
    measured = replace(frame(discharged_kwh=2), battery_start={}, battery_end={})
    flows = measured_cash_flows(measured, bindings())
    assert flows == {"actual": (1, 0), "without_battery": (3, 0)}
    actual, _ = cash_account(
        contract(), measured, flows["actual"], None, (), prices(), None, actual=True
    )
    without, _ = cash_account(
        contract(), measured, flows["without_battery"], None, (), prices(), None
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
    actual, _ = cash_account(
        contract(), measured, flows["actual"], None, (), prices(), None, actual=True
    )
    without, _ = cash_account(
        contract(), measured, flows["without_battery"], None, (), prices(), None
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


def test_zero_bonus_payment_is_exact_while_unknown_allowance_stays_unknown():
    """An import-only .30 bill cannot depend on the unknown remaining export allowance."""
    result, counters = cash_account(contract(bonus=True), frame(), (1, 0), None, (), prices(), None)
    assert result["grid_cash"] == pytest.approx(0.30)
    assert result["bonus"] == 0
    assert counters is None


@pytest.mark.parametrize("price, expected", [("-0.30", -0.60), ("0", 0)])
def test_signed_and_zero_prices_keep_independent_cash_oracles(price, expected):
    """Two discharged kWh avoid buying at the signed rate. Negative buying prices reverse the saving."""
    measured = frame(discharged_kwh=2)
    flows = measured_cash_flows(measured, bindings())
    actual, _ = cash_account(
        contract(buy=price), measured, flows["actual"], None, (), prices(), None, actual=True
    )
    without, _ = cash_account(
        contract(buy=price), measured, flows["without_battery"], None, (), prices(), None
    )
    assert without["grid_cash"] - actual["grid_cash"] == pytest.approx(expected)


def test_dc_solar_meter_cannot_be_billed_as_measured_ac_generation():
    """A DC production counter cannot establish the AC alternative behind a hybrid converter."""
    from open_energy_planner.savings.model import PendingSettlement

    selected = bindings()
    selected["solar"][0]["measurement_boundary"] = "dc"
    with pytest.raises(PendingSettlement, match="requires measured AC solar"):
        measured_cash_flows(frame(), selected)
