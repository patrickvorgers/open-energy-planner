"""Independent future-use arithmetic and solver outcomes, without provider accuracy claims."""

from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from open_energy_planner.installation.valuation import TerminalValuation, derive
from open_energy_planner.optimization.domain import Status
from open_energy_planner.prices.model import PriceKind
from open_energy_planner.prices.storage.adapters.selection_store import SqlAlchemySelectionStore
from tests.app.contract.test_contract_prices import (
    NOW,
    Future,
    configuration,
    contract,
    input_source,
    price_series,
)
from tests.app.installation.test_installation import problem, solve


def valued(inputs):
    """Attach automatically derived immutable values without changing the independent physical inputs."""
    return replace(inputs, terminal_valuations=derive(inputs))


def test_selected_overlap_and_extended_prices_reach_valuation_without_provider_logic(tmp_path):
    """The price adapter selects 0.1 commands and a later 0.4 valuation. 120 control groups remain unchanged and original inputs stay frozen."""
    from open_energy_planner.prices.selection import ForecastSelection

    future = Future()
    base = problem(prices=(0.1,), loads=(1,), pv=(0,))
    intervals = []
    start = NOW
    for index in range(120):
        duration = 0.25 if index < 96 else 1
        intervals.append(
            replace(base.intervals[0], start=start, duration_h=duration, load_kwh=duration)
        )
        start += timedelta(hours=duration)
    future.problem = replace(base, intervals=tuple(intervals))
    primary_series = replace(
        price_series((0.1,) * 48, minutes=60, kind=PriceKind.FORECAST), source_id="primary"
    )
    alternative_series = replace(
        price_series((0.4,) * 72, minutes=60, kind=PriceKind.FORECAST), source_id="alternative"
    )
    source = input_source(
        tmp_path, series=primary_series, future=future, rules=contract(buy="market", sell="market")
    )
    cache = source.prices()
    cache.series = (primary_series, alternative_series)
    cache.selection = ForecastSelection(
        SqlAlchemySelectionStore.local(tmp_path), "NL", "primary", "alternative"
    )
    original = source.current(configuration(), NOW)
    (curve,) = original.problem.terminal_valuations
    assert curve.rate == pytest.approx(0.4)
    assert curve.price_basis == "post_horizon_prices"
    assert len({interval.control_group for interval in original.problem.intervals}) == 120
    assert all(interval.buy_price == 0.1 for interval in original.problem.intervals)
    assert all(
        tariff_evidence.source_id == "alternative" for tariff_evidence in curve.tariff_evidence
    )
    cache.selection.states = {1: {"preferred_source": "alternative"}}
    updated = source.current(configuration(), NOW)
    assert updated.problem.intervals[-1].buy_price == 0.4
    assert original.problem.intervals[-1].buy_price == 0.1
    assert updated.price_version != original.price_version
    cache.series = (primary_series,)
    fallback = source.current(configuration(), NOW).problem.terminal_valuations[0]
    assert fallback.price_basis == "trailing_window_proxy"
    assert fallback.rate == pytest.approx(0.1)


def test_proxy_uses_one_volume_weighted_average_with_a_cap_and_unvalued_reserve():
    """Half a kWh at 0.4 plus half at 0.1 is worth 0.25. Reserve and surplus have no discharge opportunity."""
    inputs = problem(prices=(0.1, 0.4), loads=(0.5, 0.5), initial=1)
    unit = inputs.batteries[0]
    inputs = replace(
        inputs,
        batteries=(replace(unit, battery=replace(unit.battery, minimum_kwh=0.2, capacity_kwh=2)),),
    )
    (curve,) = derive(inputs)
    assert curve.price_basis == "trailing_window_proxy"
    assert curve.rate == pytest.approx(0.25)
    assert curve.usable_kwh == pytest.approx(1)
    assert [curve.value(entity) for entity in (0, 0.2, 0.7, 1.2, 20)] == pytest.approx(
        (0, 0, 0.125, 0.25, 0.25)
    )


@pytest.mark.parametrize(
    "price,fee,expected", [(0.5, 0.02, 0.5 * 0.8 * 0.9 * 0.95 - 0.02), (-0.2, 0, 0), (0.01, 0.1, 0)]
)
def test_value_includes_cell_dc_converter_losses_and_future_wear(price, fee, expected):
    """One stored kWh delivers 0.8*0.9*0.95 AC. Nonpositive net future value gives no credit."""
    inputs = problem(prices=(price,), loads=(2,), pv=(0,), efficiency=0.95, initial=1)
    unit = inputs.batteries[0]
    inputs = replace(
        inputs,
        batteries=(
            replace(
                unit,
                battery=replace(unit.battery, discharge_efficiency=0.8, cycle_fee=fee),
                dc_efficiency=0.9,
            ),
        ),
    )
    (curve,) = derive(inputs)
    assert curve.value(1) == pytest.approx(expected)


def test_shared_converter_and_grid_capacity_is_not_promised_to_each_battery():
    """A shared 1 kW inverter and 0.5 kW export allowance yield 0.25 kWh per equal battery, not 0.5 each."""
    inputs = problem(prices=(0.3,), loads=(0,), pv=(0,), initial=1)
    inputs = replace(
        inputs,
        grid_export_limit_w=500,
        batteries=(inputs.batteries[0], replace(inputs.batteries[0], device_id="second")),
        intervals=(replace(inputs.intervals[0], sell_price=0.4),),
    )
    curves = derive(inputs)
    assert [candidate.value(1) for candidate in curves] == pytest.approx((0.1, 0.1))
    assert sum(candidate.usable_kwh for candidate in curves) == pytest.approx(0.5)


def test_pv_saturation_reserves_shared_inverter_headroom():
    """A 1 kW PV-filled hybrid inverter cannot also promise a future 1 kW battery discharge."""
    inputs = problem(prices=(0.4,), loads=(2,), pv=(1,), initial=1)
    assert derive(inputs)[0].value(1) == 0


def test_expected_pv_export_uses_only_remaining_connection_headroom():
    """0.8 kWh expected PV export leaves 0.2 kWh under the shared 1 kWh grid allowance."""
    inputs = problem(prices=(0.4,), loads=(0,), pv=(0,), initial=1)
    from open_energy_planner.installation.model import PVUnit
    from open_energy_planner.solar.calculation.model import SolarInverter

    inputs = replace(
        inputs,
        grid_export_limit_w=1000,
        inverters=(*inputs.inverters, SolarInverter("roof", 1000, 1)),
        pv_sources=(PVUnit("pv", "roof", False),),
        intervals=(
            replace(inputs.intervals[0], pv_kwh=0.8, pv_sources=(("pv", 0.8),), sell_price=0.5),
        ),
    )
    assert derive(inputs)[0].value(1) == pytest.approx(0.1)


def test_zero_discharge_or_export_permission_has_zero_sale_value():
    """Neither a blocked export connection nor a zero discharge rating provides a sale opportunity."""
    inputs = problem(prices=(0.4,), loads=(0,), pv=(0,), initial=1)
    inputs = replace(
        inputs, grid_export_limit_w=0, intervals=(replace(inputs.intervals[0], sell_price=0.5),)
    )
    assert derive(inputs)[0].value(1) == 0
    unit = inputs.batteries[0]
    inputs = replace(
        inputs,
        grid_export_limit_w=1000,
        batteries=(replace(unit, battery=replace(unit.battery, discharge_limit_w=0)),),
    )
    assert derive(inputs)[0].value(1) == 0


@pytest.mark.planner_scenario
def test_supplied_value_keeps_energy_when_future_use_exceeds_purchase_cost():
    """Charging 1 kWh at 0.1 costs 0.1 and earns estimated 0.4 later: the independently specified objective is -0.3."""
    inputs = problem(prices=(0.1,), loads=(0,), pv=(0,))
    inputs = replace(inputs, intervals=(replace(inputs.intervals[0], sell_price=0.4),))
    # Independent supplied value isolates the planner choice from automatic derivation.
    curve = TerminalValuation(
        "battery",
        0,
        1,
        0.4,
        inputs.intervals[0].start,
        inputs.intervals[0].start + timedelta(hours=1),
        "trailing_window_proxy",
    )
    inputs = replace(inputs, terminal_valuations=(curve,))
    result = solve(inputs)
    assert result.status == Status.OPTIMAL
    assert result.plan.flows[-1].batteries[0].end_kwh == pytest.approx(1)
    assert result.plan.costs.cash_cost == pytest.approx(0.1)
    assert result.plan.costs.terminal_value == pytest.approx(0.4)
    assert result.plan.costs.objective == pytest.approx(-0.3)


@pytest.mark.planner_scenario
def test_proxy_continuation_changes_final_inventory_without_double_counting_cash():
    """Buy 1.5 kWh at 0.1, serve both half-kWh loads and retain 0.5 at average 0.25: cash 0.15, terminal 0.125, objective 0.025."""
    inputs = valued(problem(prices=(0.1, 0.4), loads=(0.5, 0.5)))
    result = solve(inputs)
    assert result.status == Status.OPTIMAL
    assert result.plan.flows[-1].batteries[0].end_kwh == pytest.approx(0.5)
    assert result.plan.costs.cash_cost == pytest.approx(0.15)
    assert result.plan.costs.terminal_value == pytest.approx(0.125)
    assert result.plan.costs.objective == pytest.approx(0.025)


@pytest.mark.planner_scenario
def test_expensive_current_sale_can_outweigh_retaining_inventory():
    """One stored kWh sold for 0.6 beats a bounded terminal estimate of 0.1. Valuation must not force a target SoC."""
    inputs = valued(problem(prices=(0.1,), loads=(1,), pv=(0,), initial=1))
    inputs = replace(
        inputs, intervals=(replace(inputs.intervals[0], buy_price=0.7, sell_price=0.6, load_kwh=0),)
    )
    result = solve(inputs)
    assert result.plan.flows[-1].batteries[0].end_kwh == pytest.approx(0)
    assert result.plan.costs.cash_cost == pytest.approx(-0.6)
    assert result.plan.costs.terminal_value == pytest.approx(0)


def test_independent_validator_rejects_fabricated_terminal_credit():
    """Changing only reported inventory credit by one euro must fail settlement without importing solver expressions."""
    from open_energy_planner.installation.validation import validate

    inputs = valued(problem())
    result = solve(inputs)
    corrupted = replace(
        result.plan,
        costs=replace(result.plan.costs, terminal_value=result.plan.costs.terminal_value + 1),
    )
    with pytest.raises(ValueError, match="Financial totals"):
        validate(inputs, corrupted)


@pytest.mark.parametrize("hours", [24, 48])
def test_contract_source_prefers_post_horizon_prices_and_retains_their_provenance(tmp_path, hours):
    """A later 0.4 wholesale hour plus 0.1 surcharge values exactly one usable kWh at 0.5, without adding commands."""
    future = Future()
    base = problem(prices=(0.1,), loads=(1,), pv=(0,))
    future.problem = replace(
        base,
        intervals=tuple(
            replace(base.intervals[0], start=NOW + timedelta(hours=index_item), duration_h=1)
            for index_item in range(hours)
        ),
    )
    series = price_series((0.1,) * hours + (0.4,), minutes=60, kind=PriceKind.FORECAST)
    source = input_source(
        tmp_path, series=series, future=future, rules=contract(buy="market + 0.1", sell="market")
    )
    basis = source.current(configuration(), NOW)
    (curve,) = basis.problem.terminal_valuations
    assert curve.price_basis == "post_horizon_prices"
    assert curve.window_start == NOW + timedelta(hours=hours)
    assert curve.window_end == curve.window_start + timedelta(hours=1)
    assert curve.value(1) == pytest.approx(0.5)
    assert len(basis.problem.intervals) == hours * 4
    assert len({step.control_group for step in basis.problem.intervals}) == hours
    assert curve.tariff_evidence[0].source_version == series.version
    assert curve.tariff_evidence[0].price_kind == "forecast"
    assert asdict(curve)["tariff_evidence"][0]["buy_price"] == pytest.approx(0.5)


def test_expired_extension_is_not_used_as_future_price_evidence(tmp_path):
    """Expired 0.9 future prices cannot outrank a usable 0.2 current-window proxy."""
    future = Future()
    base = problem(prices=(0.2,), loads=(1,), pv=(0,))
    future.problem = replace(base, intervals=(replace(base.intervals[0], start=NOW),))
    source = input_source(
        tmp_path,
        series=price_series((0.2,), minutes=60),
        future=future,
        rules=contract(buy="market", sell="market"),
    )
    expired = replace(
        price_series((0.2, 0.9), minutes=60, kind=PriceKind.FORECAST),
        source_id="expired-extension",
        expires_at=NOW - timedelta(seconds=1),
    )
    source.prices().series = (*source.prices().series, expired)
    basis = source.current(configuration(), NOW)
    assert basis.problem.terminal_valuations[0].price_basis == "trailing_window_proxy"
    assert basis.problem.terminal_valuations[0].value(1) == pytest.approx(0.2)


def test_value_shape_and_battery_association_are_validated():
    """Non-finite values, missing identities and mismatched reserves cannot enter frozen solver input."""
    inputs = valued(problem())
    (curve,) = inputs.terminal_valuations
    for changes in (
        {"rate": float("nan")},
        {"usable_kwh": -1},
        {"window_end": curve.window_start},
        {"price_basis": "guaranteed"},
    ):
        with pytest.raises(ValueError):
            replace(curve, **changes)
    with pytest.raises(ValueError, match="match all batteries"):
        replace(inputs, terminal_valuations=(replace(curve, device_id="missing"),))
    with pytest.raises(ValueError, match="match all batteries"):
        replace(inputs, terminal_valuations=(replace(curve, reserve_kwh=0.5),))


def test_no_battery_creates_no_inventory_credit():
    """A grid/PV-only installation has no remaining storage and needs no artificial terminal item."""
    inputs = replace(problem(), batteries=())
    assert derive(inputs) == ()


def test_small_negative_solver_roundoff_has_zero_inventory_value():
    """The physical validator admits -1e-7 kWh roundoff. Pricing it must yield zero, while real negative/non-finite energy fails."""
    (valuation,) = derive(problem())
    assert valuation.value(-1e-7) == 0
    assert valuation.value(-1e-9) == 0
    for value in (-1e-6, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            valuation.value(value)


@pytest.mark.parametrize("start_offset,end_offset", [(1, 2), (0, 0), (0, -1), (0, 25)])
def test_future_tariff_coverage_rejects_gaps_empty_reverse_or_excess_windows(
    start_offset, end_offset
):
    """Only positive contiguous coverage from the horizon end, bounded to 24 hours, may supply terminal prices."""
    from open_energy_planner.contracts.model import TariffEvidence

    inputs = problem(prices=(0.2,), loads=(1,), pv=(0,))
    end = inputs.intervals[0].start + timedelta(hours=1)
    tariff = TariffEvidence(
        end + timedelta(hours=start_offset),
        end + timedelta(hours=end_offset),
        0.1,
        0.2,
        0.1,
        "forecast",
        60,
        "test",
        "source-v1",
        "contract-v1",
        0,
    )
    with pytest.raises(ValueError, match="contiguous bounded"):
        derive(inputs, (tariff,))


def test_full_mixed_resolution_horizon_uses_only_last_day_and_keeps_120_steps():
    """The first 96 expensive quarters must not raise the last 24 cheap hours' 0.2 inventory rate."""
    base = problem(prices=(1,), loads=(1,), pv=(0,))
    steps = []
    for index in range(120):
        offset, duration = (index / 4, 0.25) if index < 96 else (index - 72, 1)
        steps.append(
            replace(
                base.intervals[0],
                start=NOW + timedelta(hours=offset),
                duration_h=duration,
                load_kwh=duration,
                buy_price=1 if index < 96 else 0.2,
            )
        )
    inputs = replace(base, intervals=tuple(steps))
    (valuation,) = derive(inputs)
    assert len(inputs.intervals) == 120
    assert valuation.window_start == NOW + timedelta(hours=24)
    assert valuation.window_end == NOW + timedelta(hours=48)
    assert valuation.rate == pytest.approx(0.2)


def test_average_is_weighted_by_usable_internal_volume_not_segment_count():
    """A quarter with 0.25 kWh at 0.4 and an hour with 1 kWh at 0.2 average to 0.24, not 0.3."""
    inputs = problem(prices=(0.4, 0.2), loads=(0.25, 1), initial=1)
    first, second = inputs.intervals
    inputs = replace(
        inputs,
        intervals=(
            replace(first, duration_h=0.25),
            replace(second, start=first.start + timedelta(minutes=15)),
        ),
    )
    (valuation,) = derive(inputs)
    assert valuation.rate == pytest.approx(0.24)
    assert valuation.usable_kwh == pytest.approx(1)


@pytest.mark.parametrize("discharge", [0, 0.25, 0.5, 0.75, 1])
def test_independent_settlement_prices_each_allowed_inventory_choice(discharge):
    """Every d kWh discharged costs 0.3*(1-d) now and retains 0.2*(1-d). Objective is 0.1*(1-d)."""
    from open_energy_planner.installation.model import (
        BatteryFlow,
        InstallationFlow,
        InstallationPlan,
        InverterFlow,
        PVFlow,
    )
    from open_energy_planner.installation.validation import validate
    from open_energy_planner.optimization.domain import Costs

    inputs = valued(problem(prices=(0.2,), loads=(1,), pv=(0,), initial=1))
    inputs = replace(inputs, intervals=(replace(inputs.intervals[0], buy_price=0.3),))
    remaining = 1 - discharge
    costs = Costs(0.3 * remaining, 0, 0, 0, 0.3 * remaining, 0.2 * remaining, 0.1 * remaining)
    flow = InstallationFlow(
        remaining,
        0,
        (BatteryFlow("battery", 0, discharge, remaining),),
        (InverterFlow("inverter", 0, discharge, False),),
        (PVFlow("pv", 0, True),),
    )
    checked = validate(inputs, InstallationPlan((flow,), costs))
    assert checked.objective == pytest.approx(0.1 * remaining)


def test_open_ended_price_rule_also_values_energy_beyond_command_horizon(tmp_path):
    """The last rule stays valid beyond commands: the later 0.9 price values remaining energy without extending control."""
    future = Future()
    base = problem(prices=(0.2,), loads=(1,), pv=(0,))
    future.problem = replace(base, intervals=(replace(base.intervals[0], start=NOW),))
    rules = contract(buy="market", sell="market")
    source = input_source(
        tmp_path,
        future=future,
        rules=rules,
        series=price_series((0.2, 0.9), minutes=60, kind=PriceKind.FORECAST),
    )
    basis = source.current(configuration(), NOW)
    (valuation,) = basis.problem.terminal_valuations
    assert valuation.price_basis == "post_horizon_prices"
    assert valuation.rate == pytest.approx(0.9)
    assert len(basis.problem.intervals) == 4
    assert len({step.control_group for step in basis.problem.intervals}) == 1


def test_future_price_selection_changes_input_version_even_when_cached_values_do_not(tmp_path):
    """Changing only forecast issue precedence selects 0.5 instead of 0.4 later and must change the planning input version."""
    future = Future()
    base = problem(prices=(0.2,), loads=(1,), pv=(0,))
    future.problem = replace(base, intervals=(replace(base.intervals[0], start=NOW),))
    source = input_source(
        tmp_path,
        future=future,
        rules=contract(buy="market", sell="market"),
        series=price_series((0.2,), minutes=60),
    )
    tail = price_series((0.4,), minutes=60, kind=PriceKind.FORECAST)
    first = replace(
        tail,
        source_id="first",
        issued_at=NOW - timedelta(minutes=1),
        points=(
            replace(tail.points[0], start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2)),
        ),
    )
    second = replace(
        first,
        source_id="second",
        issued_at=NOW - timedelta(minutes=2),
        points=(replace(first.points[0], value=0.5),),
    )
    cache = source.prices()
    cache.series = (*cache.series, first, second)
    version = cache.version
    before = source.current(configuration(), NOW)
    cache.series = (*cache.series[:-1], replace(second, issued_at=NOW))
    after = source.current(configuration(), NOW)
    assert cache.version == version
    assert before.problem.terminal_valuations[0].rate == pytest.approx(0.4)
    assert after.problem.terminal_valuations[0].rate == pytest.approx(0.5)
    assert before.price_version != after.price_version
