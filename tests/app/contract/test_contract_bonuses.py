"""Independent rule, window, counter-evidence and exact billing projection checks."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from open_energy_planner.contracts.bonus import BonusRule, CounterBook, CounterSeed
from open_energy_planner.contracts.bonus_statistics import StatisticReference, StatisticsBasis
from open_energy_planner.contracts.calculation.condition import (
    ComparisonCondition,
    ComparisonOperator,
)
from open_energy_planner.contracts.calculation.formula import Formula
from open_energy_planner.contracts.storage import ContractStore
from open_energy_planner.contracts.windows import ClockWindow
from open_energy_planner.contracts.wire import parse_contract
from open_energy_planner.home_assistant.observation_wire import parse_configuration, parse_report
from open_energy_planner.home_assistant.observations import InputsUnavailable, SnapshotAssembler
from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import Battery, Interval
from open_energy_planner.planning import CommandMapper
from open_energy_planner.storage.counters import CounterStore
from tests.app.contract.support.settings import settings
from tests.app.contract.test_contract_prices import (
    NOW,
    Future,
    contract,
    input_source,
    price_series,
)
from tests.database.support.archive import corrupt_document, make_store, read_document
from tests.ha_contract.test_observation_inputs import report_body


def rule(**changes):
    """Create one explicit synthetic capped bonus recorded before the test horizon."""
    return BonusRule("solar", datetime(2026, 1, 1, tzinfo=UTC), Formula("0.04"), 100, **changes)


def measured_inputs(*, meter=50, used=99.6, now=NOW, location=None):
    """Pair a confirmed annual baseline with fresh cumulative export, not a planned volume."""
    config = parse_configuration(
        {
            "bindings": [
                {"key": "grid.power", "entity_id": "sensor.grid", "quantity": "power"},
                {"key": "grid.export_energy", "entity_id": "sensor.export", "quantity": "energy"},
            ]
        },
        revision="sources-bonus",
    )
    body = report_body(config, now=now)
    body["readings"][1].update(value=meter, unit="kWh")
    if location:
        body["location"] = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "timezone": location.timezone,
        }
    report = parse_report(body, config, now)
    endpoint = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    basis = StatisticsBasis(
        endpoint - timedelta(hours=1),
        endpoint,
        (StatisticReference("sensor.export", 1, "recorder", "kWh", 50, meter),),
        used,
        now,
        "a" * 64,
        True,
    )
    seed = CounterSeed(
        "solar",
        rule().period_start(now),
        used + meter - 50,
        meter,
        "sensor.export",
        endpoint,
        (("sensor.export", meter),),
        basis,
    )
    return config, report, CounterBook(now, (seed,))


@pytest.mark.parametrize("market,expected", [(-0.1, 0), (0, 0), (0.1, 0.05)])
def test_positive_price_gate_and_percentage_are_separate(market, expected):
    """A 50% positive-market bonus pays 0.05 at 0.10 and never magnifies a negative base price."""
    current = replace(
        rule(),
        rate_formula=Formula("0.5 * max(market, 0)"),
        price_condition=ComparisonCondition(
            Formula("market"), ComparisonOperator.GREATER_THAN, Decimal(0)
        ),
    )
    assert current.extra_rate(market, NOW) == pytest.approx(expected)


@pytest.mark.parametrize(
    "hour,minute,expected", [(5, 59, 0), (6, 0, 0.04), (21, 59, 0.04), (22, 0, 0)]
)
def test_clock_window_is_half_open(hour, minute, expected):
    """Include 06:00 but exclude 22:00. The payment gate does not change counter advancement."""
    current = rule(window="clock", start_time="06:00", end_time="22:00", timezone="UTC")
    assert current.extra_rate(0.1, NOW.replace(hour=hour, minute=minute)) == expected


def test_overnight_and_repeated_dst_hour_have_explicit_edges():
    """A 22-06 window crosses midnight. A 02:15 autumn gate opens in both repeated hours."""
    overnight = rule(window="clock", start_time="22:00", end_time="06:00", timezone="UTC")
    assert overnight.extra_rate(0.1, NOW.replace(hour=23)) == 0.04
    assert overnight.extra_rate(0.1, NOW.replace(hour=4)) == 0.04
    assert overnight.extra_rate(0.1, NOW.replace(hour=12)) == 0
    repeated = rule(window="clock", start_time="02:15", end_time="02:45")
    start = datetime(2026, 10, 25, tzinfo=UTC)
    edges = repeated.boundaries(start, start + timedelta(hours=3))
    assert edges == tuple(start + timedelta(minutes=value) for value in (15, 45, 75, 105))


def test_spring_dst_gap_does_not_create_nonexistent_edges():
    """The missing 02:15 edge is skipped. The offset jump opens payment at 03:00 and 03:15 closes it."""
    current = rule(window="clock", start_time="02:15", end_time="03:15")
    start = datetime(2026, 3, 29, tzinfo=UTC)
    assert current.boundaries(start, start + timedelta(hours=3)) == (
        start + timedelta(minutes=60),
        start + timedelta(minutes=75),
    )


def test_daylight_uses_ha_coordinates_and_local_date(monkeypatch):
    """Use HA's date/timezone even when the contractual counter timezone differs. Boundaries remain exact."""
    seen = []

    def daylight(latitude, longitude, day, timezone):
        """Supply a fixed sunrise/sunset oracle without duplicating Astral's astronomical algorithm."""
        seen.append((latitude, longitude, day, timezone))
        start = datetime(day.year, day.month, day.day, 6, tzinfo=UTC)
        return start, start + timedelta(hours=12)

    monkeypatch.setattr("open_energy_planner.contracts.windows.solar_window", daylight)
    location = SiteLocation(52, 6, "Europe/Amsterdam")
    current = rule(window="daylight", timezone="America/Los_Angeles")
    assert current.extra_rate(0.1, NOW, location=location) == 0.04
    assert seen[-1] == (52, 6, NOW.date(), "Europe/Amsterdam")
    current.extra_rate(0.1, NOW.replace(hour=1), location=location)
    assert seen[-1][2] == NOW.date()  # The counter timezone is still on the previous day.
    assert current.boundaries(NOW.replace(hour=0), NOW.replace(hour=23), location=location) == (
        NOW.replace(hour=6),
        NOW.replace(hour=18),
    )
    with pytest.raises(ValueError, match="location"):
        current.extra_rate(0.1, NOW)


def test_real_local_sunrise_is_an_absolute_ordered_window():
    """Calculate Netherlands solar events locally and verify the half-open payment boundary."""
    current = rule(window="daylight")
    location = SiteLocation(52, 6, "Europe/Amsterdam")
    start = NOW.replace(hour=0)
    rise, setting = current.boundaries(start, start + timedelta(hours=24), location=location)
    assert start < rise < NOW < setting < start + timedelta(hours=24)
    assert current.extra_rate(0.1, rise - timedelta(seconds=1), location=location) == 0
    assert current.extra_rate(0.1, rise, location=location) == 0.04
    assert current.extra_rate(0.1, setting, location=location) == 0


def test_confirmed_meter_delta_replanning_and_snapshot_evidence(tmp_path):
    """A 0.2 measured increase leaves 0.2 quota. Repeated plans preserve the seed and archive exact provenance."""
    config, report, book = measured_inputs(meter=50.2)
    source = input_source(tmp_path, rules=replace(contract(), bonuses=(rule(),)))
    source.counters = lambda: book
    first = source.current(config, NOW, report=report)
    second = source.current(config, NOW, report=report)
    assert first == second
    assert first.problem.export_bonuses[0].initial_kwh == pytest.approx(99.8)
    assert book.seeds[0].statistics.prefix_kwh == 99.6
    assert first.bonus_evidence[0].meter_kwh == 50.2
    snapshot = SnapshotAssembler().build(config, report, first, NOW)
    assert snapshot.record()["bonus_evidence"][0]["counter_version"] == book.version


@pytest.mark.parametrize(
    "fault", ["missing_seed", "source", "statistics_changed", "stale", "optional"]
)
def test_unusable_counter_evidence_blocks_bonus_planning(tmp_path, fault):
    """Never invent a fresh bonus budget from missing, stale, reset or differently bound meter data."""
    config, report, book = measured_inputs()
    if fault == "missing_seed":
        book = None
    elif fault == "source":
        config = replace(
            config,
            bindings=(config.bindings[0], replace(config.bindings[1], entity_id="sensor.other")),
        )
    elif fault == "statistics_changed":
        seed = book.seeds[0]
        book = replace(
            book,
            seeds=(
                replace(
                    seed,
                    statistics=replace(
                        seed.statistics, issue="bonus_statistics_revalidation_required"
                    ),
                ),
            ),
        )
    elif fault == "stale":
        seed = book.seeds[0]
        endpoint = NOW - timedelta(hours=4)
        book = replace(
            book,
            seeds=(
                replace(
                    seed,
                    observed_at=endpoint,
                    statistics=replace(
                        seed.statistics,
                        latest_at=endpoint,
                        reference_at=endpoint - timedelta(hours=1),
                    ),
                ),
            ),
        )
    else:
        config = replace(
            config, bindings=(config.bindings[0], replace(config.bindings[1], required=False))
        )
    source = input_source(tmp_path, rules=replace(contract(), bonuses=(rule(),)))
    source.counters = lambda: book
    with pytest.raises(InputsUnavailable, match="bonus_"):
        source.current(config, NOW, report=report)


@pytest.mark.parametrize("with_seed", [False, True])
def test_missing_current_prices_take_precedence_over_bonus_readiness(tmp_path, with_seed):
    """Missing current prices report tariff_coverage_gap, regardless of whether bonus baselines exist."""
    config, report, book = measured_inputs()
    prices = price_series()
    prices = replace(prices, points=prices.points[1:])
    source = input_source(tmp_path, rules=replace(contract(), bonuses=(rule(),)), series=prices)
    source.counters = lambda: book if with_seed else None
    with pytest.raises(InputsUnavailable, match="^tariff_coverage_gap$"):
        source.current(config, NOW, report=report)


def test_unexpected_bonus_formula_failure_has_stable_reason(tmp_path):
    """A formula undefined at the actual 0.2 market value reports bonus_rules_invalid, never raw arithmetic text."""
    config, report, book = measured_inputs()
    current = replace(rule(), rate_formula=Formula("0.01 / abs(market - 0.2)"))
    source = input_source(
        tmp_path, rules=replace(contract(), bonuses=(current,)), series=price_series((0.2, 0.2))
    )
    source.counters = lambda: book
    with pytest.raises(InputsUnavailable, match="^bonus_rules_invalid$"):
        source.current(config, NOW, report=report)


def test_dated_versions_change_rate_and_cap_without_reset(tmp_path):
    """The newest same-ID version wins at its instant. 99.6 used kWh survive both rate and cap changes."""
    config, report, book = measured_inputs()
    later = replace(
        rule(),
        effective_from=NOW + timedelta(minutes=15),
        cap_kwh=101,
        rate_formula=Formula("0.06"),
    )
    source = input_source(tmp_path, rules=replace(contract(), bonuses=(later, rule())))
    source.counters = lambda: book
    prepared = source.current(config, NOW, report=report).problem.export_bonuses[0]
    assert prepared.rates == (0.04, 0.06)
    assert prepared.caps_kwh == (100, 101)
    assert prepared.initial_kwh == 99.6
    assert len(set(prepared.periods)) == 1


def test_mid_quarter_gate_preserves_one_command_and_exact_bonus(tmp_path):
    """A 12:07:30 gate leaves half of a uniformly forecast kWh eligible: 0.5*0.04=0.02, one command."""
    config, report, book = measured_inputs(used=0)
    later = replace(rule(), effective_from=NOW + timedelta(minutes=7, seconds=30))
    future = Future((Interval(NOW, 0.25, 999, 999, pv_kwh=1),))
    source = input_source(
        tmp_path, rules=replace(contract(sell="0"), bonuses=(later,)), future=future
    )
    source.counters = lambda: book
    basis = source.current(config, NOW, report=report)
    snapshot = SnapshotAssembler().build(config, report, basis, NOW)
    result = HighsSolver().solve(snapshot.problem)
    costs = validate(snapshot.problem, result.plan)
    assert costs.bonus_income == pytest.approx(0.02)
    commands = CommandMapper().build(snapshot, result, costs, NOW)["steps"]
    assert len(commands) == 1
    assert commands[0]["end"] == (NOW + timedelta(minutes=15)).isoformat()


def test_unequal_tariffs_cannot_create_an_extra_battery_switch(tmp_path):
    """One half-hour command cannot exploit opposite quarter tariffs. Equal charging yields zero arbitrage."""
    future = Future((Interval(NOW, 0.5, 999, 999),), battery=Battery(2, 1))
    source = input_source(
        tmp_path,
        rules=contract(buy="market", sell="market"),
        series=price_series((-0.1, 0.1)),
        future=future,
    )
    basis = source.current(measured_inputs()[0], NOW)
    result = HighsSolver().solve(basis.problem)
    costs = validate(basis.problem, result.plan)
    assert costs.cash_cost == pytest.approx(0)
    flows = result.plan.flows
    assert flows[0].batteries[0].charge_kwh == pytest.approx(flows[1].batteries[0].charge_kwh)
    assert flows[0].batteries[0].discharge_kwh == pytest.approx(flows[1].batteries[0].discharge_kwh)


def test_counter_and_bonus_rule_revisions_survive_restart(tmp_path):
    """Persist separate immutable counter/rule revisions and recover them with unchanged content identities."""
    book = measured_inputs()[2]
    counters = make_store(tmp_path, CounterStore)
    counters.save(book)
    newer = replace(book, recorded_at=NOW + timedelta(seconds=1))
    counters.save(newer)
    restored = make_store(tmp_path, CounterStore)
    assert restored.current == newer
    assert restored.load(book.version) == book
    rules = replace(contract(), bonuses=(rule(),))
    store = make_store(tmp_path, ContractStore)
    store.save(rules)
    assert make_store(tmp_path, ContractStore).current == rules


@pytest.mark.parametrize("window", ["clock", "monthly_schedule"])
def test_48_hour_billing_refinement_keeps_120_commands(tmp_path, window):
    """Refine 96 quarters plus 24 hours for exact tariffs/windows without creating additional commands."""
    steps = tuple(
        Interval(NOW + timedelta(minutes=15 * index), 0.25, 999, 999, pv_kwh=0.25)
        for index in range(96)
    ) + tuple(
        Interval(NOW + timedelta(hours=24 + index), 1, 999, 999, pv_kwh=1) for index in range(24)
    )
    current = rule(window="clock", start_time="06:07", end_time="22:07", timezone="UTC")
    if window == "monthly_schedule":
        current = replace(
            current,
            window=window,
            start_time=None,
            end_time=None,
            monthly_schedule=(ClockWindow("06:07", "22:07"),) * 12,
        )
    config, report, book = measured_inputs(used=0)
    book = replace(book, seeds=(replace(book.seeds[0], period_start=current.period_start(NOW)),))
    source = input_source(
        tmp_path,
        rules=replace(contract(sell="0"), bonuses=(current,)),
        series=price_series((0,) * 192),
        future=Future(steps),
    )
    source.counters = lambda: book
    basis = source.current(config, NOW, report=report)
    snapshot = SnapshotAssembler().build(config, report, basis, NOW)
    result = HighsSolver().solve(basis.problem)
    costs = validate(basis.problem, result.plan)
    assert len(basis.problem.intervals) > 192
    assert sum(step.pv_kwh for step in basis.problem.intervals) == pytest.approx(48)
    assert costs.bonus_income == pytest.approx(32 * 0.04)  # Two 16-hour daily windows.
    assert len(CommandMapper().build(snapshot, result, costs, NOW)["steps"]) == 120


def test_contract_year_boundary_and_future_reset_keep_measured_initial_volume(tmp_path):
    """An October-21 contract year resets tomorrow. The current measured 99.6 kWh remain until that instant."""
    config, report, book = measured_inputs()
    current = replace(
        rule(reset_month=10, reset_day=21, timezone="UTC"),
        effective_from=datetime(2025, 10, 21, tzinfo=UTC),
    )
    seed = replace(book.seeds[0], period_start=datetime(2025, 10, 21, tzinfo=UTC))
    book = replace(book, seeds=(seed,))
    future = Future(
        (Interval(NOW, 12, 999, 999), Interval(NOW + timedelta(hours=12), 12, 999, 999))
    )
    source = input_source(
        tmp_path,
        rules=replace(contract(), bonuses=(current,)),
        series=price_series((0,) * 24, minutes=60),
        future=future,
    )
    source.counters = lambda: book
    prepared = source.current(config, NOW, report=report).problem.export_bonuses[0]
    assert prepared.initial_kwh == 99.6
    assert prepared.periods[47] == seed.period_start.isoformat()
    assert prepared.periods[48] == datetime(2026, 10, 21, tzinfo=UTC).isoformat()


def test_failed_counter_pointer_write_preserves_active_revision(tmp_path, monkeypatch):
    """An interrupted pointer update cannot activate an uncommitted counter. The previous archive remains current."""

    store = make_store(tmp_path, CounterStore)
    book = measured_inputs()[2]
    store.save(book)
    real = store.documents.write

    def fail_pointer(target, body):
        """Fail only publication, after the candidate archive was durably written."""
        if target == store.current_name:
            raise OSError("fixture pointer failure")
        real(target, body)

    monkeypatch.setattr(store.documents, "write", fail_pointer)
    with pytest.raises(OSError):
        store.save(replace(book, recorded_at=NOW + timedelta(seconds=1)))
    assert store.current == make_store(tmp_path, CounterStore).current == book


@pytest.mark.parametrize("content", ["null", "[]", '"../escape"'])
def test_corrupt_counter_archive_is_reported_without_deletion(tmp_path, content):
    """Unusable counter history blocks readiness instead of silently replacing the real budget with zero."""
    store = make_store(tmp_path, CounterStore)
    book = measured_inputs()[2]
    store.save(book)
    path = f"{book.version}.json"
    corrupt_document(tmp_path, "bonus-counters", path, content)
    restored = make_store(tmp_path, CounterStore)
    assert restored.current is None
    assert restored.issue == "bonus_counter_cache_unreadable"
    assert read_document(tmp_path, "bonus-counters", path) == content


@pytest.mark.parametrize(
    "change", [{"routes": ["pv"]}, {"cap_kwh": -1}, {"rate_formula": "-0.1"}, {"window": "script"}]
)
def test_invalid_bonus_configuration_is_rejected(change):
    """Reject removed source filters, negative amounts and arbitrary window constructs at the wire boundary."""
    body = settings()
    body["bonuses"] = [
        {
            "rule_id": "solar",
            "effective_from": NOW.isoformat(),
            "rate_formula": "0.04",
            "cap_kwh": 100,
            **change,
        }
    ]
    with pytest.raises((ValueError, TypeError)):
        parse_contract(body, NOW)


@pytest.mark.parametrize(
    ("day", "expected"),
    [("2026-01-01", "2025-12-31T23:00:00+00:00"), ("2026-07-01", "2026-06-30T22:00:00+00:00")],
)
def test_contract_calendar_date_resolves_at_home_assistant_midnight(day, expected):
    """Winter/summer installation dates resolve at local midnight rather than UTC or the browser zone."""
    from open_energy_planner.contracts.wire import effective_instant

    assert effective_instant(day, "Europe/Amsterdam").astimezone(UTC).isoformat() == expected


def test_contract_calendar_date_rejects_nonexistent_midnight_and_preserves_absolute_time():
    """A skipped calendar midnight is invalid. An existing explicit timestamp retains its exact instant."""
    from open_energy_planner.contracts.wire import effective_instant

    with pytest.raises(ValueError, match="no midnight"):
        effective_instant("2011-12-30", "Pacific/Apia")
    assert effective_instant("2026-07-01T12:30:00+00:00", "Europe/Amsterdam") == datetime(
        2026, 7, 1, 12, 30, tzinfo=UTC
    )


def test_latest_bonus_rule_replaces_older_rule_even_with_a_different_identity(tmp_path):
    """A .04 rule pays only the first quarter and .08 only the second. Two 1-kWh exports earn .12, never .16."""
    from open_energy_planner.savings.accounting import bonus_payment

    config, report, book = measured_inputs(used=98)
    later = replace(
        rule(),
        rule_id="new-contract-bonus",
        effective_from=NOW + timedelta(minutes=15),
        rate_formula=Formula("0.08"),
    )
    schedule = replace(contract(), bonuses=(later, rule()))
    book = replace(book, seeds=(*book.seeds, replace(book.seeds[0], rule_id=later.rule_id)))
    source = input_source(tmp_path, rules=schedule)
    source.counters = lambda: book
    snapshot = source.current(config, NOW, report=report)
    rates = {bonus.rule_id: bonus.rates for bonus in snapshot.problem.export_bonuses}
    assert rates == {"solar": (0.04, 0), "new-contract-bonus": (0, 0.08)}
    assert schedule.bonus_at(NOW).rule_id == "solar"
    assert schedule.bonus_at(NOW + timedelta(minutes=15)).rule_id == "new-contract-bonus"
    counters = {identity + "|" + rule().period_start(NOW).isoformat(): 98 for identity in rates}
    before = bonus_payment(
        schedule, NOW, NOW + timedelta(minutes=15), 0.2, 1, counters, (), {}, None
    )
    after = bonus_payment(
        schedule,
        NOW + timedelta(minutes=15),
        NOW + timedelta(minutes=30),
        0.2,
        1,
        counters,
        (),
        {},
        None,
    )
    assert before == pytest.approx(0.04)
    assert after == pytest.approx(0.08)
    assert before + after == pytest.approx(0.12)


def test_two_bonus_rules_cannot_start_at_the_same_instant():
    """Equal start dates across IDs have no unique newest rule and must be rejected instead of paying both."""
    with pytest.raises(ValueError, match="unique effective dates"):
        replace(contract(), bonuses=(rule(), replace(rule(), rule_id="other")))


@pytest.mark.parametrize("market,expected", [(-0.02, 0.016), (-0.01, 0)])
def test_comparison_solver_and_settlement_share_chronological_cap(tmp_path, market, expected):
    """At <=-0.02 the .4-kWh remaining cap pays .4*.04=.016. Other export still consumes quota."""
    from open_energy_planner.savings.accounting import bonus_payment

    config, report, book = measured_inputs()
    gated = replace(
        rule(),
        window="clock",
        start_time="06:00",
        end_time="22:00",
        price_condition=ComparisonCondition(
            Formula("market + 0.02"), ComparisonOperator.LESS_THAN_OR_EQUAL, Decimal(0)
        ),
    )
    schedule = replace(contract(sell="0"), bonuses=(gated,))
    future = Future((Interval(NOW, 0.25, 999, 999, pv_kwh=1),))
    source = input_source(
        tmp_path, rules=schedule, series=price_series((market, market)), future=future
    )
    source.counters = lambda: book
    snapshot = SnapshotAssembler().build(
        config, report, source.current(config, NOW, report=report), NOW
    )
    result = HighsSolver().solve(snapshot.problem)
    costs = validate(snapshot.problem, result.plan)
    assert costs.bonus_income == pytest.approx(expected)
    counters = {gated.rule_id + "|" + gated.period_start(NOW).isoformat(): 99.6}
    paid = bonus_payment(
        schedule, NOW, NOW + timedelta(minutes=15), market, 1, counters, (), {}, None
    )
    assert paid == pytest.approx(expected)
    assert next(iter(counters.values())) == pytest.approx(100.6)


@pytest.mark.parametrize("window", ["clock", "daylight", "monthly_schedule"])
@pytest.mark.parametrize(
    "market,used,inside,expected",
    [
        (-0.03, 98, True, 0),
        (-0.02, 98, True, 0),
        (-0.01, 98, True, 0.001),
        (0.06, 98, True, 0.008),
        (0.06, 99.6, True, 0.0032),
        (0.06, 100, True, 0),
        (0.06, 101, True, 0),
        (0.06, 99.6, False, 0),
    ],
)
def test_price_window_and_quota_jointly_control_solver_and_settlement(
    tmp_path, monkeypatch, window, market, used, inside, expected
):
    """One exported kWh pays 10% of market+0.02 only inside both gates and remaining quota.

    At market 0.06 the extra rate is 0.008. A 0.4-kWh remainder pays 0.0032.
    Failed gates still consume export quota. Both independent calculations must agree.
    """
    from open_energy_planner.savings.accounting import bonus_payment

    location = SiteLocation(52, 6, "UTC")
    config, report, book = measured_inputs(used=used, location=location)
    opening_hour = 12 if inside else 13

    def daylight(latitude, longitude, day, timezone):
        """Supply deterministic daylight edges to isolate bonus arithmetic from astronomy."""
        opening = datetime(day.year, day.month, day.day, opening_hour, tzinfo=UTC)
        return opening, opening + timedelta(hours=1)

    monkeypatch.setattr("open_energy_planner.contracts.windows.solar_window", daylight)
    gated = replace(
        rule(),
        rate_formula=Formula("0.10 * (market + 0.02)"),
        price_condition=ComparisonCondition(
            Formula("market + 0.02"), ComparisonOperator.GREATER_THAN, Decimal(0)
        ),
        window=window,
        timezone="UTC",
        start_time=f"{opening_hour:02d}:00" if window == "clock" else None,
        end_time=f"{opening_hour + 1:02d}:00" if window == "clock" else None,
        monthly_schedule=(ClockWindow(f"{opening_hour:02d}:00", f"{opening_hour + 1:02d}:00"),) * 12
        if window == "monthly_schedule"
        else (),
    )
    book = replace(book, seeds=(replace(book.seeds[0], period_start=gated.period_start(NOW)),))
    schedule = replace(contract(sell="0"), bonuses=(gated,))
    source = input_source(
        tmp_path,
        rules=schedule,
        series=price_series((market, market)),
        future=Future((Interval(NOW, 0.25, 999, 999, pv_kwh=1),)),
    )
    source.counters = lambda: book
    snapshot = SnapshotAssembler().build(
        config, report, source.current(config, NOW, report=report), NOW
    )
    result = HighsSolver().solve(snapshot.problem)
    costs = validate(snapshot.problem, result.plan)
    assert costs.bonus_income == pytest.approx(expected)
    counters = {gated.rule_id + "|" + gated.period_start(NOW).isoformat(): used}
    assert bonus_payment(
        schedule, NOW, NOW + timedelta(minutes=15), market, 1, counters, (), {}, location
    ) == pytest.approx(expected)
    assert next(iter(counters.values())) == pytest.approx(used + 1)


@pytest.mark.parametrize("target", ["period", "bonus"])
def test_calendar_rules_require_discovered_installation_timezone(target):
    """Calendar-only boundaries cannot silently use UTC or a supplied bonus timezone before HA discovery."""
    body = settings()
    if target == "period":
        body["periods"][0]["effective_from"] = "2026-01-01"
    else:
        body["bonuses"] = [
            {
                "rule_id": "example",
                "effective_from": "2026-01-01",
                "rate_formula": "0.03",
                "cap_kwh": 100,
                "timezone": "Europe/Amsterdam",
            }
        ]
    with pytest.raises(ValueError, match="installation timezone is required"):
        parse_contract(body, NOW)
    assert parse_contract(body, NOW, installation_timezone="Europe/Amsterdam")


@pytest.mark.parametrize(
    "seconds,microseconds,expected_minute", [(0, 0, 19), (27, 14508, 20), (0, 1, 20)]
)
def test_daylight_bonus_uses_only_complete_minutes(
    monkeypatch, seconds, microseconds, expected_minute
):
    """A 07:19:27 sunrise begins payment at 07:20. Sunset at 17:13:32 ends at 17:13. Exact minute events stay exact."""
    import open_energy_planner.contracts.windows as bonus_module

    sunrise = datetime(2026, 10, 27, 7, 19, seconds, microseconds, tzinfo=UTC)
    sunset = datetime(2026, 10, 27, 17, 13, 32, 807212, tzinfo=UTC)
    monkeypatch.setattr(bonus_module, "daylight", lambda *_args: (sunrise, sunset))
    bonus_module.solar_window.cache_clear()
    try:
        start, end = bonus_module.solar_window(52, 6, sunrise.date(), "UTC")
        assert start == datetime(2026, 10, 27, 7, expected_minute, tzinfo=UTC)
        assert end == datetime(2026, 10, 27, 17, 13, tzinfo=UTC)
        assert start >= sunrise and end <= sunset
    finally:
        bonus_module.solar_window.cache_clear()
