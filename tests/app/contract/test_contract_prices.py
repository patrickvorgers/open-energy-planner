"""Independent contract arithmetic, effective boundaries, persistence and planner scenarios."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.contracts.calculation.formula import Formula
from open_energy_planner.contracts.model import TariffCalculator
from open_energy_planner.contracts.planning_basis import ContractBasisSource
from open_energy_planner.contracts.storage import ContractStore
from open_energy_planner.contracts.wire import parse_contract
from open_energy_planner.home_assistant.observations import (
    InputsUnavailable,
    PlanningBasis,
    SnapshotAssembler,
)
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import Battery, Interval
from open_energy_planner.optimization.export_bonus import ExportBonus
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries, combine
from open_energy_planner.prices.service import PriceService
from open_energy_planner.prices.storage.adapters.store import PriceStore
from tests.app.contract.support.settings import settings
from tests.app.installation.support.problems import installation_problem
from tests.database.support.archive import corrupt_document, make_store, read_document
from tests.ha_contract.test_observation_inputs import (
    configuration,
    parse_report,
    report_body,
)

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "market, expected", [(0, 0.1308481), (0.10, 0.2518481), (-0.10, 0.0098481)]
)
def test_documented_2026_import_example_keeps_tax_precision_and_does_not_tax_the_inclusive_fee(
    market, expected
):
    """2026 tax at 0.09161 is VAT-exclusive. The 0.02 fee already includes VAT, so a zero market costs 0.1308481 rather than double-taxing the fee."""
    assert Formula("(market + 0.09161) * 1.21 + 0.02").evaluate(market) == pytest.approx(
        expected, abs=1e-12
    )


@pytest.mark.parametrize("market,expected", [(0, 0.1327481), (0.1, 0.2537481), (-0.1, 0.0117481)])
def test_nextenergy_example_adds_the_inclusive_purchase_fee_after_vat(market, expected):
    """The advertised 0.0219 fee is already inclusive. Preserve 2026 tax precision and add no second VAT charge on that fee."""
    assert Formula("(market + 0.09161) * 1.21 + 0.0219").evaluate(market) == pytest.approx(
        expected, abs=1e-12
    )


def contract(**kwargs):
    """Create a revision already recorded before the deterministic planning clock."""
    return parse_contract(settings(**kwargs), NOW - timedelta(hours=1))


def price_series(values=(0.1, 0.4), *, minutes=15, kind=PriceKind.PUBLISHED):
    """Supply explicit wholesale intervals with stable identity and native duration."""
    return PriceSeries(
        "price-fixture",
        "NL",
        "EUR",
        NOW,
        NOW,
        None,
        tuple(
            PricePoint(
                NOW + timedelta(minutes=minutes * index_item),
                NOW + timedelta(minutes=minutes * (index_item + 1)),
                value,
                kind,
                minutes,
            )
            for index_item, value in enumerate(values)
        ),
    )


class Future:
    """Supply declared future energy/limits while tariff placeholders must be overwritten."""

    def __init__(self, intervals=None, *, battery=None):
        """Freeze deterministic forecasts with conspicuously incorrect placeholder prices."""
        self.problem = installation_problem(
            intervals
            or (
                Interval(NOW, 0.25, 999, 999),
                Interval(NOW + timedelta(minutes=15), 0.25, 999, 999),
            ),
            battery=battery,
        )

    def current(self, config, now, *, report=None):
        """Return an independently named forecast revision and bounded freshness."""
        return PlanningBasis(
            config.revision,
            "forecast-fixture",
            "unused-placeholder",
            NOW + timedelta(hours=1),
            self.problem,
        )


def input_source(tmp_path, *, rules=None, series=None, future=None):
    """Compose real price-cache orchestration and the contract adapter without network or HA."""
    store = make_store(tmp_path, PriceStore)
    store.save((series or price_series(),))
    prices = PriceService("NL", None, None, store)
    return ContractBasisSource(future or Future(), lambda: prices, lambda: rules or contract())


@pytest.mark.parametrize(
    "market,buy,sell", [(-0.2, -0.121, -0.22), (0, 0.121, -0.02), (0.2, 0.363, 0.18)]
)
def test_import_and_export_have_independently_checked_signs(market, buy, sell):
    """Apply a 0.10 surcharge then 21% tax to import, and subtract 0.02 from export revenue."""
    rules = contract()
    assert rules.periods[0].import_formula.evaluate(market) == pytest.approx(buy)
    assert rules.periods[0].export_formula.evaluate(market) == pytest.approx(sell)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("min(market, 0)", -0.2),
        ("max(0, market)", 0),
        ("abs(market)", 0.2),
        ("-market + +0.1", 0.3),
        ("market / 2", -0.1),
        ("0.1 + 0.2", 0.3),
    ],
)
def test_safe_arithmetic_functions_and_decimal_literals(text, expected):
    """Evaluate supported arithmetic at -0.2. Decimal 0.1 + 0.2 converts to exactly the same float as 0.3."""
    assert Formula(text).evaluate(-0.2) == expected


@pytest.mark.parametrize(
    "text",
    [
        "__import__('os')",
        "market.real",
        "market[0]",
        "[market]",
        "lambda: market",
        "market ** 2",
        "market > 0",
        "True",
        "unknown + 1",
        "sum(market)",
        "max(market, key=abs)",
        "abs(market, 0)",
        "max()",
        "1e999",
        "0x10",
        "1e-99999999999999999999999999999999",
        "9" * 400,
        "market + " * 100 + "0",
        "",
    ],
)
def test_unsupported_formula_syntax_is_rejected_before_use(text):
    """Reject executable, ambiguous or oversized syntax rather than executing or simplifying it."""
    with pytest.raises(ValueError):
        Formula(text)


@pytest.mark.parametrize("text,market", [("1 / market", 0), ("1000000 * market", 2)])
def test_undefined_or_excessive_actual_prices_are_not_zero(text, market):
    """Reject division by zero or an excessive calculated rate rather than inventing a free tariff."""
    with pytest.raises(ValueError):
        Formula(text).evaluate(market)


def test_preview_is_explicit_and_does_not_guarantee_all_future_prices():
    """Preview standard examples, then reject a different actual price at the formula's singularity."""
    rules = contract(buy="1 / (market - 0.3)")
    assert len(rules.preview()[0]) == 3
    with pytest.raises(ValueError):
        rules.periods[0].import_formula.evaluate(0.3)
    with pytest.raises(ValueError):
        contract(buy="1 / market")


def test_contract_period_boundary_selects_new_rules_at_exact_instant():
    """Split one wholesale hour at 12:30. The half-open new period charges 0.5 instead of 0.3."""
    rules = contract(buy="market + 0.1")
    first = replace(rules.periods[0], effective_until=NOW + timedelta(minutes=30))
    second = replace(
        first,
        effective_from=first.effective_until,
        effective_until=None,
        import_formula=Formula("market + 0.3"),
    )
    rules = replace(rules, periods=(first, second))
    result = TariffCalculator().calculate(
        combine((price_series((0.2,), minutes=60),), NOW, market_area="NL", hours=1), rules
    )
    assert [(item.start, item.end, item.buy_price, item.period_index) for item in result] == [
        (NOW, NOW + timedelta(minutes=15), 0.3, 0),
        (NOW + timedelta(minutes=15), NOW + timedelta(minutes=30), 0.3, 0),
        (NOW + timedelta(minutes=30), NOW + timedelta(minutes=45), 0.5, 1),
        (NOW + timedelta(minutes=45), NOW + timedelta(hours=1), 0.5, 1),
    ]


@pytest.mark.parametrize(
    "fault", ["overlap", "unordered", "gap", "naive", "currency", "area", "bonus"]
)
def test_unsupported_contracts_and_missing_coverage_fail_explicitly(fault):
    """Reject ambiguous periods, missing coverage/identity and unsupported bonus fields without applying defaults."""
    body = settings()
    if fault in ("overlap", "unordered"):
        body["periods"] *= 2
    elif fault == "gap":
        body["periods"][0]["effective_from"] = (NOW + timedelta(hours=1)).isoformat()
    elif fault == "naive":
        body["periods"][0]["effective_from"] = "2026-01-01T00:00:00"
    elif fault in ("currency", "area"):
        body["currency" if fault == "currency" else "market_area"] = "wrong"
    else:
        body["bonus"] = {"rate": 1}
    with pytest.raises((ValueError, TypeError)):
        rules = parse_contract(body, NOW)
        TariffCalculator().calculate(combine((price_series(),), NOW, market_area="NL"), rules)


def test_archive_corrections_preserve_original_rule_text_and_reference(tmp_path):
    """Correct past formula text in a new revision. Reload both original and corrected rules independently."""
    store = make_store(tmp_path, ContractStore)
    old = contract()
    store.save(old)
    original = read_document(tmp_path, "contracts", f"{old.version}.json")
    corrected = replace(contract(buy="market + 0.05"), recorded_at=NOW)
    store.save(corrected)
    assert store.load(old.version) == old
    assert store.load(corrected.version) == corrected
    assert make_store(tmp_path, ContractStore).current == corrected
    assert read_document(tmp_path, "contracts", f"{old.version}.json") == original
    assert corrected.version != old.version


def test_failed_pointer_save_does_not_publish_new_contract(tmp_path, monkeypatch):
    """Fail the final atomic pointer replacement. Retain the previous current revision in memory and on disk."""
    store = make_store(tmp_path, ContractStore)
    old, new = contract(), contract(buy="market")
    store.save(old)
    replace_file = store._replace

    def fail_pointer(path, body):
        """Allow immutable archive creation but fail advancement of the authoritative pointer."""
        if path == store.current_name:
            raise OSError("fixture disk failure")
        replace_file(path, body)

    monkeypatch.setattr(store, "_replace", fail_pointer)
    with pytest.raises(OSError):
        store.save(new)
    assert store.current == make_store(tmp_path, ContractStore).current == old


@pytest.mark.parametrize("data", ["null", "[]", "42", '"../outside"'])
def test_structurally_corrupt_contract_cache_is_reported_without_deletion(tmp_path, data):
    """Reject unusable references or archive objects while preserving all original bytes for diagnosis."""
    store = make_store(tmp_path, ContractStore)
    store.save(contract())
    corrupt_document(tmp_path, "contracts", f"{contract().version}.json", data)
    recovered = make_store(tmp_path, ContractStore)
    assert recovered.current is None
    assert recovered.issue == "contract_cache_unreadable"
    assert read_document(tmp_path, "contracts", f"{contract().version}.json") == data


def test_planning_basis_uses_effective_rates_and_preserves_exact_evidence(tmp_path):
    """Overwrite 999 placeholders with 0.242/0.605 import costs and preserve source/rule evidence in the snapshot."""
    config = configuration()
    source = input_source(tmp_path)
    basis = source.current(config, NOW)
    assert [step.buy_price for step in basis.problem.intervals] == [0.242, 0.605]
    assert [step.sell_price for step in basis.problem.intervals] == [0.08, 0.38]
    report = parse_report(report_body(now=NOW), config, NOW)
    snapshot = SnapshotAssembler().build(config, report, basis, NOW)
    assert snapshot.contract_version == contract().version
    assert snapshot.price_version != "unused-placeholder"
    record = snapshot.record()
    assert record["tariff_evidence"][0]["market_price"] == 0.1
    assert record["tariff_evidence"][0]["source_version"] == price_series().version
    assert record["tariff_evidence"][0]["price_kind"] == "published"


def test_corrected_contract_changes_future_version_without_mutating_old_snapshot(tmp_path):
    """Apply a correction to new planning only. Old snapshot bytes and effective rates remain fixed."""
    config = configuration()
    source = input_source(tmp_path)
    old = source.current(config, NOW)
    report = parse_report(report_body(), config, NOW)
    snapshot = SnapshotAssembler().build(config, report, old, NOW)
    encoded = json.dumps(snapshot.record(), sort_keys=True)
    source.contract = lambda: contract(buy="market")
    new = source.current(config, NOW)
    assert new.price_version != old.price_version
    assert new.problem.intervals[0].buy_price == 0.1
    assert old.problem.intervals[0].buy_price == 0.242
    assert json.dumps(snapshot.record(), sort_keys=True) == encoded


def test_effective_version_tracks_selected_rates_not_receipt_or_rolling_time(tmp_path):
    """Changing issue precedence changes selected tariffs/version. Receipt-only refresh and a rolling start do not."""
    source = input_source(tmp_path)
    prices = source.prices()
    first = replace(price_series(), issued_at=NOW - timedelta(hours=2))
    second = replace(
        price_series((0.2, 0.2)), source_id="second", issued_at=NOW - timedelta(hours=3)
    )
    prices.series = (first, second)
    semantic = prices.version
    original = source.current(configuration(), NOW)
    prices.series = (first, replace(second, issued_at=NOW - timedelta(hours=1)))
    assert prices.version == semantic
    revised = source.current(configuration(), NOW)
    assert revised.problem.intervals[0].buy_price == pytest.approx(0.363)
    assert original.problem.intervals[0].buy_price == pytest.approx(0.242)
    assert revised.price_version != original.price_version
    prices.series = tuple(
        replace(item, received_at=NOW + timedelta(seconds=1)) for item in prices.series
    )
    refreshed = source.current(configuration(), NOW + timedelta(seconds=1))
    assert refreshed.price_version == revised.price_version


def test_partial_step_and_shortened_price_horizon_preserve_energy_rates(tmp_path):
    """Start halfway through a quarter with only that quarter priced. Retain half its forecast energy and source expiry."""
    future = Future((Interval(NOW, 0.25, 999, 999, load_kwh=0.4),))
    prices = replace(
        price_series((0.1,), kind=PriceKind.FORECAST), expires_at=NOW + timedelta(minutes=10)
    )
    source = input_source(tmp_path, series=prices, future=future)
    result = source.current(configuration(), NOW + timedelta(minutes=7, seconds=30))
    assert result.problem.intervals[0].duration_h == 0.125
    assert result.problem.intervals[0].load_kwh == 0.2
    assert result.valid_until == NOW + timedelta(minutes=10)


def test_heterogeneous_prices_inside_coarse_step_are_not_silently_averaged(tmp_path):
    """Keep distinct quarter tariffs within one half-hour control group instead of averaging costs."""
    source = input_source(tmp_path, future=Future((Interval(NOW, 0.5, 999, 999),)))
    result = source.current(configuration(), NOW)
    assert [step.buy_price for step in result.problem.intervals] == [0.242, 0.605]
    assert len({step.control_group for step in result.problem.intervals}) == 1
    assert len(result.tariff_evidence) == 2


def test_equal_rates_with_multiple_evidence_segments_keep_one_control_step(tmp_path):
    """Keep a half-hour control step when both quarter tariffs are equal, retaining two billing-source segments."""
    source = input_source(
        tmp_path, series=price_series((0.1, 0.1)), future=Future((Interval(NOW, 0.5, 999, 999),))
    )
    result = source.current(configuration(), NOW)
    assert len(result.problem.intervals) == 2
    assert len({step.control_group for step in result.problem.intervals}) == 1
    assert len(result.tariff_evidence) == 2


def test_price_and_contract_boundary_inside_current_quarter_blocks_approximation(tmp_path):
    """Split a seven-minute contract boundary for billing while preserving the original quarter's control group."""
    rules = contract(buy="market")
    first = replace(rules.periods[0], effective_until=NOW + timedelta(minutes=7))
    second = replace(
        first,
        effective_from=first.effective_until,
        effective_until=None,
        import_formula=Formula("market + 0.1"),
    )
    source = input_source(tmp_path, rules=replace(rules, periods=(first, second)))
    result = source.current(configuration(), NOW)
    steps = result.problem.intervals
    assert [step.buy_price for step in steps] == pytest.approx([0.1, 0.2, 0.5])
    assert [step.duration_h for step in steps] == pytest.approx([7 / 60, 8 / 60, 0.25])
    assert steps[0].control_group == steps[1].control_group != steps[2].control_group


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("contract", "contract_not_configured"),
        ("prices", "market_prices_unavailable"),
        ("forecasts", "forecasts_not_configured"),
        ("expired", "tariff_coverage_gap"),
        ("formula", "contract_or_market_prices_invalid"),
    ],
)
def test_missing_or_unusable_components_block_planning_without_defaults(tmp_path, fault, reason):
    """Remove a required pricing component. Expose the exact readiness reason and never use placeholder rates."""
    source = input_source(tmp_path)
    if fault == "contract":
        source.contract = lambda: None
    elif fault == "prices":
        source.prices = lambda: None
    elif fault == "forecasts":
        source.future = None
    elif fault == "formula":
        source.contract = lambda: contract(buy="1 / (market - 0.4)")
    else:
        prices = source.prices()
        prices.series = (replace(price_series(kind=PriceKind.FORECAST), expires_at=NOW),)
    with pytest.raises(InputsUnavailable, match=reason):
        source.current(configuration(), NOW)


def test_rolling_reads_do_not_churn_effective_price_version(tmp_path):
    """Read the same source/rules a minute later. Clipping changes evidence times but not the semantic price revision."""
    source = input_source(tmp_path)
    first = source.current(configuration(), NOW)
    later = source.current(configuration(), NOW + timedelta(minutes=1))
    assert first.price_version == later.price_version
    assert first.tariff_evidence[0].start != later.tariff_evidence[0].start


def test_forecast_source_cannot_inject_an_unconfigured_bonus(tmp_path):
    """Only saved contract rules supply compensation, even when an injected forecast contains a bonus."""
    source = input_source(tmp_path)
    source.future.problem = replace(
        source.future.problem,
        export_bonuses=(ExportBonus("unconfigured", (0.1, 0.1), (1, 1), ("year", "year"), 0),),
    )
    with pytest.raises(InputsUnavailable, match="unsupported_contract_bonus"):
        source.current(configuration(), NOW)


@pytest.mark.parametrize("with_selection", [False, True])
def test_mixed_48_hour_grid_retains_all_120_control_steps(tmp_path, with_selection):
    """The 96+24 control grid keeps exact tariffs. Moving lead-bucket splits do not create a new effective price revision."""
    intervals, points = [], []
    cursor = NOW
    for index in range(120):
        duration = 0.25 if index < 96 else 1
        end = cursor + timedelta(hours=duration)
        intervals.append(Interval(cursor, duration, 999, 999, load_kwh=duration))
        points.append(
            PricePoint(
                cursor,
                end,
                0.1 if index < 96 else 0.2,
                PriceKind.PUBLISHED if index < 96 else PriceKind.FORECAST,
                15 if index < 96 else 60,
            )
        )
        cursor = end
    source = input_source(
        tmp_path,
        series=replace(price_series(), points=tuple(points)),
        future=Future(tuple(intervals)),
    )
    prices = source.prices()
    if with_selection:
        from open_energy_planner.prices.selection import ForecastSelection
        from open_energy_planner.prices.storage.adapters.selection_store import (
            SqlAlchemySelectionStore,
        )

        prices.selection = ForecastSelection(
            SqlAlchemySelectionStore(prices.store.documents.engine),
            "NL",
            "price-fixture",
            "alternative",
        )
    result = source.current(configuration(), NOW)
    assert len(result.problem.intervals) == 192
    assert len({step.control_group for step in result.problem.intervals}) == 120
    assert sum(step.duration_h for step in result.problem.intervals) == 48
    assert sum(step.load_kwh for step in result.problem.intervals) == 48
    assert result.problem.intervals[95].buy_price == 0.242
    assert result.problem.intervals[96].buy_price == 0.363
    assert result.tariff_evidence[96].price_kind == "forecast"
    later = source.current(configuration(), NOW + timedelta(minutes=5))
    assert later.price_version == result.price_version
    assert later.tariff_evidence[0].start != result.tariff_evidence[0].start
    if with_selection:
        assert later.tariff_evidence[96].start == result.tariff_evidence[96].start
    original = prices.series[0]
    changed = list(original.points)
    changed[96] = replace(changed[96], value=0.3)
    prices.series = (replace(original, points=tuple(changed)),)
    corrected = source.current(configuration(), NOW + timedelta(minutes=5))
    assert corrected.price_version != later.price_version
    assert corrected.problem.intervals[96].buy_price == 0.484


@pytest.mark.planner_scenario
def test_effective_tariffs_change_optimal_battery_decision_and_independent_cost(tmp_path):
    """A 0.5 import surcharge makes 0.6/0.9 buys exceed 0.1/0.4 sales, so cycling an empty battery loses money and idle costs zero."""
    battery = Battery(1, 0, charge_limit_w=4000, discharge_limit_w=4000)
    future = Future(battery=battery)
    source = input_source(
        tmp_path, rules=contract(buy="market + 0.5", sell="market"), future=future
    )
    problem = source.current(configuration(), NOW).problem
    result = HighsSolver().solve(problem)
    costs = validate(problem, result.plan)
    assert costs.objective == pytest.approx(0)
    assert all(
        flow.batteries[0].charge_kwh == 0 and flow.batteries[0].discharge_kwh == 0
        for flow in result.plan.flows
    )


@pytest.mark.planner_scenario
def test_negative_effective_import_tariff_rewards_exactly_one_kwh(tmp_path):
    """A -0.2 wholesale quarter plus 0.1 surcharge pays 0.1 for one kWh. The next zero-revenue quarter preserves the zero-cost terminal stock."""
    battery = Battery(1, 0, charge_limit_w=4000, discharge_limit_w=0)
    source = input_source(
        tmp_path,
        rules=contract(buy="market + 0.1", sell="0"),
        series=price_series((-0.2, 0.4)),
        future=Future(battery=battery),
    )
    problem = source.current(configuration(), NOW).problem
    result = HighsSolver().solve(problem)
    costs = validate(problem, result.plan)
    assert costs.import_cost == pytest.approx(-0.1)
    assert result.plan.flows[0].batteries[0].charge_kwh == pytest.approx(1)
    assert result.plan.flows[-1].batteries[0].end_kwh == pytest.approx(1)


@pytest.mark.parametrize("fault", ["expiry", "gap", "same_start"])
def test_price_rules_cannot_expire_or_leave_a_gap_after_the_first_start(fault):
    """A new start replaces the old tariff. A separate final expiry, gap or equal start cannot leave ambiguous coverage."""
    current = contract()
    first = replace(current.periods[0], effective_until=NOW)
    second = replace(current.periods[0], effective_from=NOW)
    periods = (first, second)
    if fault == "expiry":
        periods = (first,)
    elif fault == "gap":
        periods = (first, replace(second, effective_from=NOW + timedelta(minutes=15)))
    else:
        periods = (
            replace(first, effective_from=NOW, effective_until=NOW + timedelta(minutes=15)),
            second,
        )
    with pytest.raises(ValueError):
        replace(current, periods=periods)


def test_quarter_prices_keep_clock_hour_control_groups_and_exact_48_hour_energy(tmp_path):
    """A :15 start keeps 99 quarters, 23 full hours and a final quarter. Billing retains all 192 quarters.

    Every hour has one control group despite four billing rows. Forty-eight
    one-kW hours remain 48 kWh after refinement. Native hourly price evidence
    stays hourly in provenance and no hourly control begins at :15 or :45.
    """
    from open_energy_planner.planning_policy import planning_grid

    start = NOW + timedelta(minutes=15)
    future = Future(
        tuple(
            Interval(begin, duration, 999, 999, load_kwh=duration)
            for begin, duration in planning_grid(start)
        )
    )
    source = input_source(tmp_path, series=price_series((0.1,) * 49, minutes=60), future=future)
    result = source.current(configuration(), start)
    steps = result.problem.intervals
    assert len(steps) == 192
    assert sum(step.duration_h for step in steps) == 48
    assert sum(step.load_kwh for step in steps) == 48
    assert steps[-1].start + timedelta(hours=steps[-1].duration_h) == start + timedelta(hours=48)
    groups = {}
    for step in steps:
        groups.setdefault(step.control_group, []).append(step)
    controls = list(groups.values())
    assert len(controls) == 123
    assert all(len(group) == 1 for group in controls[:99])
    assert all(len(group) == 4 and group[0].start.minute == 0 for group in controls[99:-1])
    assert len(controls[-1]) == 1
    assert all(evidence.native_resolution_minutes == 60 for evidence in result.tariff_evidence)


def test_price_preview_uses_exact_paid_window_and_price_condition_without_volume():
    """At .10 market the .012 bonus pays only 12:15-12:20 UTC. Negative market and other times keep the base rate, even with a zero cap."""
    from decimal import Decimal

    from open_energy_planner.contracts.bonus import BonusRule
    from open_energy_planner.contracts.calculation.condition import (
        ComparisonCondition,
        ComparisonOperator,
    )
    from open_energy_planner.contracts.compilation import BonusCompiler

    bonus = BonusRule(
        "preview",
        NOW - timedelta(days=1),
        Formula("0.10 * (market + 0.02)"),
        0,
        timezone="UTC",
        window="clock",
        start_time="12:10",
        end_time="12:20",
        price_condition=ComparisonCondition(
            Formula("market + 0.02"), ComparisonOperator.GREATER_THAN, Decimal(0)
        ),
    )
    schedule = replace(contract(), bonuses=(bonus,))
    timeline = combine((price_series((-0.05, 0.10)),), NOW, market_area="NL", hours=0.5)
    tariffs = TariffCalculator().calculate(timeline, schedule)
    preview = BonusCompiler().preview(schedule, tariffs)
    assert [(row.tariff.start.minute, row.tariff.end.minute) for row in preview] == [
        (0, 10),
        (10, 15),
        (15, 20),
        (20, 30),
    ]
    assert [row.bonus_rate for row in preview] == pytest.approx([0, 0, 0.012, 0])
    assert [row.tariff.sell_price + row.bonus_rate for row in preview] == pytest.approx(
        [-0.07, -0.07, 0.092, 0.08]
    )
    assert all(row.issue is None for row in preview)


def test_price_preview_retains_base_prices_when_daylight_location_is_missing():
    """Unknown daylight leaves an unknown bonus. It cannot erase a known .08 export tariff or masquerade as a zero bonus."""
    from open_energy_planner.contracts.bonus import BonusRule
    from open_energy_planner.contracts.compilation import BonusCompiler

    schedule = replace(
        contract(),
        bonuses=(
            BonusRule("preview", NOW - timedelta(days=1), Formula("0.03"), 7500, window="daylight"),
        ),
    )
    timeline = combine((price_series((0.1,)),), NOW, market_area="NL", hours=0.25)
    preview = BonusCompiler().preview(schedule, TariffCalculator().calculate(timeline, schedule))
    assert len(preview) == 1
    assert preview[0].tariff.sell_price == pytest.approx(0.08)
    assert preview[0].bonus_rate is None
    assert preview[0].issue == "bonus_window_unavailable"


def test_unknown_future_bonus_window_does_not_hide_known_current_bonus():
    """The first quarter pays a known .03. A later daylight rule without location is unknown only after its own effective boundary."""
    from open_energy_planner.contracts.bonus import BonusRule
    from open_energy_planner.contracts.compilation import BonusCompiler

    schedule = replace(
        contract(),
        bonuses=(
            BonusRule("old", NOW - timedelta(days=1), Formula("0.03"), 7500),
            BonusRule("new", NOW + timedelta(minutes=15), Formula("0.04"), 7500, window="daylight"),
        ),
    )
    timeline = combine((price_series((0.1, 0.1)),), NOW, market_area="NL", hours=0.5)
    preview = BonusCompiler().preview(schedule, TariffCalculator().calculate(timeline, schedule))
    assert [row.bonus_rate for row in preview] == [0.03, None]
    assert preview[0].issue is None
    assert preview[1].issue == "bonus_window_unavailable"
