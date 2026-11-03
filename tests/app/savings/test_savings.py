"""Independent self-consumption, billing and continuous benefit scenarios with explicit arithmetic."""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.contracts.bonus import CounterSeed
from open_energy_planner.installation.model import PVUnit
from open_energy_planner.savings.accounting import evaluate
from open_energy_planner.savings.context import physical_context
from open_energy_planner.savings.model import Frame, parse_frame
from open_energy_planner.savings.reference import dispatch
from open_energy_planner.solar.calculation.model import SolarInverter
from tests.app.savings.support.scenarios import (
    END,
    START,
    bindings,
    contract,
    frame,
    prices,
    problem,
)


def test_reference_discharge_covers_load_without_price_decision():
    """One kWh load consumes one stored kWh even at negative export price. No grid trade occurs."""
    planning_problem = problem()
    flow = dispatch(planning_problem, planning_problem.intervals[0], {"battery": 2})
    assert flow["stored"] == {"battery": 1}
    assert flow["import_kwh"] == flow["export_kwh"] == 0


def test_reference_keeps_pv_enabled_and_exports_even_when_it_costs_money():
    """A full battery and 1-kWh production with no load exports the full kWh, costing 0.10."""
    planning_problem = problem(pv_kwh=1, load_kwh=0, hybrid=True)
    flow = dispatch(planning_problem, planning_problem.intervals[0], {"battery": 4})
    assert flow["export_kwh"] == 1
    assert flow["stored"] == {"battery": 4}
    assert flow["export_kwh"] * planning_problem.intervals[0].sell_price == -0.1


def test_reference_dc_clipping_charge_obeys_shared_converter_limit():
    """1.5 DC kWh at 80% produces 1 AC kWh and stores the otherwise clipped 0.25 DC kWh."""
    planning_problem = problem(pv_kwh=1.5, load_kwh=1, hybrid=True, efficiency=0.8)
    flow = dispatch(planning_problem, planning_problem.intervals[0], {"battery": 2})
    assert flow["stored"]["battery"] == pytest.approx(2.25)
    assert flow["import_kwh"] == flow["export_kwh"] == 0


def test_actual_costs_use_measurements_and_reference_remains_continuous():
    """Actual import costs .30, reference uses .20 inventory. Benefit is -.10, then -.10 again."""
    planning_problem = problem()
    first, state = evaluate(
        planning_problem, frame(), bindings(), None, contract(), (), prices(), None
    )
    assert first["actual"]["grid_cash"] == pytest.approx(0.3)
    assert first["reference"]["grid_cash"] == 0
    assert first["reference"]["inventory_change"] == pytest.approx(-0.2)
    assert first["savings"] == pytest.approx(-0.1)
    second, _ = evaluate(
        planning_problem, frame(start=END), bindings(), state, contract(), (), prices(END), None
    )
    assert second["reference"]["stored"] == {"battery": 0}
    assert second["savings"] == pytest.approx(-0.1)


def test_gap_never_resets_reference_or_fabricates_savings():
    """Skipping a quarter must fail. Restarting the reference would grant another free battery."""
    planning_problem = problem()
    _, state = evaluate(planning_problem, frame(), bindings(), None, contract(), (), prices(), None)
    with pytest.raises(ValueError, match="continuity"):
        evaluate(
            planning_problem,
            frame(start=END + timedelta(minutes=15)),
            bindings(),
            state,
            contract(),
            (),
            prices(),
            None,
        )


def test_bonus_caps_advance_all_export_and_diverge_by_scenario():
    """Both inherit 1.5 of 2 kWh used. .5 actual export gets .05 while reference stores the surplus."""
    planning_problem = problem(pv_kwh=0.5, load_kwh=0, hybrid=True)
    seed = CounterSeed("bonus", datetime(2026, 1, 1, tzinfo=UTC), 1.5, 10, "sensor.export", START)
    result, state = evaluate(
        planning_problem,
        frame(imported_kwh=0, exported_kwh=0.5, pv_kwh=0.5),
        bindings(),
        None,
        contract(bonus=True),
        (seed,),
        prices(),
        None,
    )
    assert result["actual"]["bonus"] == pytest.approx(0.05)
    assert result["reference"]["bonus"] == 0
    assert list(state["counters"]["actual"].values()) == [2]
    assert list(state["counters"]["reference"].values()) == [1.5]


def test_missing_published_prices_and_nan_are_rejected():
    """Incomplete price coverage cannot become zero cost. Nonfinite measurements cannot enter the archive."""
    with pytest.raises(ValueError, match="coverage"):
        evaluate(problem(), frame(), bindings(), None, contract(), (), [], None)
    bad = frame().record()
    bad["channels"]["sensor.import"] = float("nan")
    with pytest.raises(ValueError, match="energy"):
        parse_frame(bad)
    missing = replace(frame(), channels={})
    with pytest.raises(ValueError, match="meter"):
        evaluate(problem(), missing, bindings(), None, contract(), (), prices(), None)


def test_original_plan_physics_reconstruct_without_solver():
    """JSON serialization retains multiple-device identifiers and terminal curves exactly for reference calculation."""
    import json

    planning_problem = problem(hybrid=True)
    value = json.loads(
        json.dumps(asdict(planning_problem), default=lambda value_item: value_item.isoformat())
    )
    rebuilt = physical_context({"input_snapshot": {"problem": value}})
    assert rebuilt == planning_problem


def test_hybrid_charges_local_and_other_inverter_surplus_without_creating_energy():
    """0.1 local plus 0.5 remote kWh stores exactly 0.6, leaving no grid trade or unexplained energy."""
    planning_problem = problem(pv_kwh=0.1, load_kwh=0, hybrid=True)
    planning_problem = replace(
        planning_problem,
        inverters=(*planning_problem.inverters, SolarInverter("other", 4000, 1, False)),
        pv_sources=(*planning_problem.pv_sources, PVUnit("garage", "other", False)),
        intervals=(
            replace(
                planning_problem.intervals[0],
                pv_kwh=0.6,
                pv_sources=(("roof", 0.1), ("garage", 0.5)),
            ),
        ),
    )
    result = dispatch(planning_problem, planning_problem.intervals[0], {"battery": 2})
    assert result["stored"]["battery"] == pytest.approx(2.6)
    assert result["import_kwh"] == 0
    assert result["export_kwh"] == pytest.approx(0)


def test_shared_converter_cannot_create_dc_clipping_while_charging_from_ac():
    """Two batteries behind one converter can absorb one remote kWh once. Negative AC flow is not clipped PV."""
    planning_problem = problem(pv_kwh=0, load_kwh=0, hybrid=True)
    second = replace(planning_problem.batteries[0], device_id="second")
    planning_problem = replace(
        planning_problem,
        inverters=(*planning_problem.inverters, SolarInverter("other", 4000, 1, False)),
        batteries=(*planning_problem.batteries, second),
        pv_sources=(*planning_problem.pv_sources, PVUnit("garage", "other", False)),
        intervals=(
            replace(
                planning_problem.intervals[0], pv_kwh=1, pv_sources=(("roof", 0), ("garage", 1))
            ),
        ),
        terminal_valuations=(),
    )
    result = dispatch(planning_problem, planning_problem.intervals[0], {"battery": 2, "second": 2})
    assert sum(result["stored"].values()) == pytest.approx(5)
    assert result["export_kwh"] == 0


def test_removed_battery_inventory_is_not_reported_as_savings():
    """Removing unequal stored energy posts each scenario's loss externally, so hardware removal creates zero benefit."""
    planning_problem = problem()
    _, previous = evaluate(
        planning_problem, frame(), bindings(), None, contract(), (), prices(), None
    )
    without = replace(planning_problem, batteries=(), terminal_valuations=())
    settlement_bindings = {**bindings(), "batteries": []}
    measured = Frame(
        END,
        END + timedelta(minutes=15),
        {"sensor.import": 0, "sensor.export": 0, "sensor.pv": 0, "sensor.discharge": 0},
        {},
        {},
        {},
        "recorded",
    )
    result, _ = evaluate(
        without, measured, settlement_bindings, previous, contract(), (), prices(END), None
    )
    assert result["savings"] == pytest.approx(0)
    assert sorted(measurement["kwh"] for measurement in result["inventory_movements"]) == [-2, -1]
    assert result["actual"]["inventory_change"] == pytest.approx(0)
    assert result["reference"]["inventory_change"] == pytest.approx(0)


def test_negative_import_prices_can_improve_money_and_increase_grid_import():
    """Paid import preserves .20 inventory and earns .20 cash, versus .20 reference inventory use: +.40 and -1 kWh avoided."""
    result, _ = evaluate(
        problem(), frame(), bindings(), None, contract(buy="-0.2"), (), prices(), None
    )
    assert result["actual"]["grid_cash"] == pytest.approx(-0.2)
    assert result["savings"] == pytest.approx(0.4)
    assert result["avoided_import_kwh"] == -1


def test_reference_uses_real_dc_efficiency_advantage():
    """One DC kWh stores 1 kWh locally, versus .64 through two 80% AC converters."""
    hybrid = problem(pv_kwh=1, load_kwh=0, hybrid=True, efficiency=0.8)
    ac = replace(
        hybrid,
        inverters=(
            SolarInverter("inv", 4000, 0.8, False),
            SolarInverter("battery-inv", 4000, 0.8, False),
        ),
        batteries=(replace(hybrid.batteries[0], inverter_id="battery-inv"),),
    )
    direct = dispatch(hybrid, hybrid.intervals[0], {"battery": 2})
    converted = dispatch(ac, ac.intervals[0], {"battery": 2})
    assert direct["stored"]["battery"] == pytest.approx(3)
    assert converted["stored"]["battery"] == pytest.approx(2.64)


def test_optional_battery_needs_no_soc_and_grid_only_comparison_is_zero():
    """Without storage or PV both scenarios import one kWh at .30, so no SoC or invented savings is needed."""
    planning_problem = problem()
    planning_problem = replace(
        planning_problem,
        batteries=(),
        pv_sources=(),
        inverters=(),
        terminal_valuations=(),
        intervals=(replace(planning_problem.intervals[0], pv_kwh=0, pv_sources=()),),
    )
    measured = Frame(START, END, {"sensor.import": 1}, {}, {}, {"sensor.import": 10}, "recorded")
    settlement_bindings = {
        "terms": [{"entity_id": "sensor.import", "sign": 1}],
        "imports": ["sensor.import"],
        "exports": [],
        "solar": [],
        "batteries": [],
    }
    result, _ = evaluate(
        planning_problem, measured, settlement_bindings, None, contract(), (), prices(), None
    )
    assert result["savings"] == 0
    assert result["actual"]["grid_cash"] == result["reference"]["grid_cash"] == 0.3


def test_zero_available_solar_at_night_is_not_missing_forecast():
    """A requested off state at night with fully covered zero-yield input still permits valid settlement."""
    planning_problem = problem()
    planning_problem = replace(
        planning_problem, pv_sources=(replace(planning_problem.pv_sources[0], can_curtail=True),)
    )
    settlement_bindings = {**bindings(), "curtailed": {"roof": True}}
    result, _ = evaluate(
        planning_problem, frame(), settlement_bindings, None, contract(), (), prices(), None
    )
    assert result["savings"] == pytest.approx(-0.1)
    assert result["available_pv_estimated"]


@pytest.mark.parametrize("kind", ["solar", "batteries"])
def test_missing_physical_binding_is_a_descriptive_settlement_error(kind):
    """Missing PV/battery associations fail explicitly. StopIteration must never escape through asyncio.to_thread."""
    selected = bindings()
    selected[kind] = []
    with pytest.raises(ValueError, match="binding is missing"):
        evaluate(problem(), frame(), selected, None, contract(), (), prices(), None)
