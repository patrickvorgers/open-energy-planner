"""Independent monthly eligibility, exact segmentation and strict JSON checks."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from open_energy_planner.contracts.compilation import BonusCompiler
from open_energy_planner.contracts.model import TariffCalculator
from open_energy_planner.contracts.windows import BonusWindow, ClockWindow
from open_energy_planner.contracts.wire import contract_document, parse_contract
from open_energy_planner.prices.model import combine
from tests.app.contract.support.settings import settings
from tests.app.contract.test_contract_bonuses import rule
from tests.app.contract.test_contract_prices import NOW, contract, price_series


def monthly(**months):
    """Build an explicitly disabled schedule except for the supplied month numbers."""
    return BonusWindow(
        "monthly_schedule",
        monthly_schedule=tuple(months.get(str(month)) for month in range(1, 13)),
    )


@pytest.mark.parametrize(
    "instant,expected",
    [
        ("2026-01-31T22:00:00+00:00", True),
        ("2026-02-01T05:59:59+00:00", True),
        ("2026-02-01T06:00:00+00:00", False),
        ("2026-02-01T12:00:00+00:00", False),
        ("2026-12-31T23:00:00+00:00", True),
        ("2027-01-01T03:59:59+00:00", True),
        ("2027-01-01T04:00:00+00:00", False),
    ],
)
def test_opening_month_owns_overnight_tail(instant, expected):
    """January's 22-06 and December's 23-04 ranges carry into the next month/year exactly."""
    window = monthly(**{"1": ClockWindow("22:00", "06:00"), "12": ClockWindow("23:00", "04:00")})
    assert window.contains(datetime.fromisoformat(instant), timezone="UTC") is expected


@pytest.mark.parametrize(
    "start,opening,closing,expected_hours",
    [
        ("2026-03-29T00:00:00+00:00", "02:15", "03:15", 0.25),
        ("2026-10-25T00:00:00+00:00", "02:15", "02:45", 1),
        ("2026-10-25T00:00:00+00:00", "02:15", "03:15", 1.75),
        ("2026-10-25T00:00:00+00:00", "01:00", "04:00", 3),
    ],
)
def test_clock_and_monthly_have_identical_real_dst_payment(start, opening, closing, expected_hours):
    """Literal real-hour totals include autumn repetitions and exclude the missing spring hour."""
    start = datetime.fromisoformat(start)
    end = start + timedelta(hours=4)
    clock = BonusWindow("clock", opening, closing)
    scheduled = BonusWindow(
        "monthly_schedule", monthly_schedule=(ClockWindow(opening, closing),) * 12
    )
    for window in (clock, scheduled):
        edges = (start, *window.boundaries(start, end, timezone="Europe/Amsterdam"), end)
        paid_hours = sum(
            (right - left).total_seconds() / 3600
            for left, right in pairwise(edges)
            if window.contains(left + (right - left) / 2, timezone="Europe/Amsterdam")
        )
        assert paid_hours == expected_hours
        # Each billing segment must be uniform even immediately around a DST jump.
        for left, right in pairwise(edges):
            assert window.contains(left, timezone="Europe/Amsterdam") == window.contains(
                right - timedelta(microseconds=1), timezone="Europe/Amsterdam"
            )


@pytest.mark.parametrize("year,day", [(2026, 28), (2028, 29)])
def test_february_tail_and_march_opening_create_distinct_exact_edges(year, day):
    """The last February night closes at 06:00 while March opens at 08:00, including leap years."""
    window = monthly(**{"2": ClockWindow("22:00", "06:00"), "3": ClockWindow("08:00", "10:00")})
    start = datetime(year, 2, day, 21, tzinfo=UTC)
    assert window.boundaries(start, start + timedelta(hours=15), timezone="UTC") == (
        datetime(year, 2, day, 22, tzinfo=UTC),
        datetime(year, 3, 1, 6, tzinfo=UTC),
        datetime(year, 3, 1, 8, tzinfo=UTC),
        datetime(year, 3, 1, 10, tzinfo=UTC),
    )


def test_monthly_compiler_preserves_literal_bonus_income_and_tariffs():
    """At one kW export, 12:07-12:23 pays 16/60 * 0.04 EUR, independently of base tariffs."""
    current = replace(
        rule(timezone="UTC"),
        window="monthly_schedule",
        monthly_schedule=(ClockWindow("12:07", "12:23"),) * 12,
    )
    schedule = replace(contract(), bonuses=(current,))
    prices = TariffCalculator().calculate(
        combine((price_series(),), NOW, market_area="NL", hours=0.5), schedule
    )
    preview = BonusCompiler().preview(schedule, prices)
    assert [item.bonus_rate for item in preview] == [0, 0.04, 0.04, 0]
    assert sum(
        (item.tariff.end - item.tariff.start).total_seconds() / 3600 * item.bonus_rate
        for item in preview
    ) == pytest.approx(0.04 * 16 / 60)
    assert [item.tariff.buy_price for item in preview] == pytest.approx(
        [0.242, 0.242, 0.605, 0.605]
    )


def monthly_body():
    """Supply a complete API document with an October-only overnight payment range."""
    body = settings()
    body["bonuses"] = [
        {
            "rule_id": "solar",
            "effective_from": "2026-01-01T00:00:00+00:00",
            "rate_formula": "0.04",
            "cap_kwh": 100,
            "window": "monthly_schedule",
            "monthly_schedule": [None] * 9
            + [{"start_time": "22:00", "end_time": "06:00"}]
            + [None] * 2,
        }
    ]
    return body


def test_monthly_wire_round_trip_preserves_exact_immutable_schedule():
    """API serialization retains disabled months, rule identity and the complete version hash."""
    original = parse_contract(monthly_body(), NOW)
    document = contract_document(original)
    assert (
        document["bonuses"][0]["monthly_schedule"]
        == monthly_body()["bonuses"][0]["monthly_schedule"]
    )
    for key in ("status", "version", "preview", "bonus_preview", "recorded_at"):
        document.pop(key)
    assert parse_contract(document, NOW) == original
    assert parse_contract(document, NOW).version == original.version


@pytest.mark.parametrize(
    "schedule",
    [
        None,
        {},
        [],
        [None] * 11,
        [None] * 13,
        [False] * 12,
        ["06:00"] * 12,
        [{"start_time": "06:00"}] * 12,
        [{"start_time": "06:00", "end_time": "06:00"}] * 12,
        [{"start_time": "6:00", "end_time": "22:00"}] * 12,
        [{"start_time": "25:00", "end_time": "22:00"}] * 12,
    ],
)
def test_monthly_wire_rejects_incomplete_or_ambiguous_ranges(schedule):
    """Trust-boundary admission rejects malformed schedules before a contract can be activated."""
    body = monthly_body()
    body["bonuses"][0]["monthly_schedule"] = schedule
    with pytest.raises(ValueError):
        parse_contract(body, NOW)


@pytest.mark.parametrize("kind", ["always", "daylight", "clock"])
def test_other_strategies_reject_monthly_fields(kind):
    """Hidden browser fields cannot silently activate a schedule on another strategy."""
    body = monthly_body()
    body["bonuses"][0]["window"] = kind
    if kind == "clock":
        body["bonuses"][0].update(start_time="06:00", end_time="22:00")
    with pytest.raises(ValueError):
        parse_contract(body, NOW)
