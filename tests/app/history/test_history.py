"""Independent history coverage and bonus-budget arithmetic without HA or solver output."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
from open_energy_planner.contracts.bonus_history import BonusHistory
from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.history.wire import parse_history
from tests.app.history.support.bonus import NOW, START, history, inputs
from tests.database.support.archive import make_store, read_document


def test_reset_aware_hourly_history_derives_missing_seed_once():
    """Four measured hours consume 4*2=8 kWh despite a physical reset. A physical settlement seed cannot suppress annual sum refreshes."""
    service = BonusHistory()
    contract, config = inputs()
    request = service.request(contract, config, None, NOW)
    assert request.end == START + timedelta(hours=4)
    book = service.derive(request, history(), None, NOW)
    assert book.seeds[0].used_kwh == 8
    assert book.seeds[0].meter_kwh == 5
    assert service.request(contract, config, book, NOW) is not None


@pytest.mark.parametrize("explicit_sources", [False, True])
def test_single_source_component_identity_is_checked_when_supplied(explicit_sources):
    """Optional physical evidence must match the sole requested meter, including legacy requests."""
    service = BonusHistory()
    contract, configuration = inputs()
    request = service.request(contract, configuration, None, NOW)
    if not explicit_sources:
        request = replace(request, entity_ids=())
    for entity_id in ("sensor.export", "sensor.wrong"):
        evidence = replace(history(), component_meters=((entity_id, 5),))
        if entity_id == "sensor.wrong":
            with pytest.raises(ValueError, match="History components changed"):
                service.derive(request, evidence, None, NOW)
        else:
            seed = service.derive(request, evidence, None, NOW).seeds[0]
            assert seed.used_kwh == 8
            assert seed.component_meters == (("sensor.export", 5),)


@pytest.mark.parametrize("missing", [0, 1, 3, 4])
def test_missing_hour_or_boundary_never_becomes_zero(missing):
    """Remove an anchor, interior or final hour. Incomplete period evidence cannot seed a counter."""
    complete = history()
    broken = replace(complete, hours=complete.hours[:missing] + complete.hours[missing + 1 :])
    with pytest.raises(ValueError, match="Incomplete"):
        broken.baseline(START, START + timedelta(hours=4))


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), -1, 10**400])
def test_invalid_meter_values_are_rejected(value):
    """Missing, boolean, nonfinite or negative physical meters remain invalid rather than fabricated readings."""
    with pytest.raises(ValueError):
        EnergyHour(START, 10, value)


def test_decreasing_statistics_and_unaligned_period_are_rejected():
    """A counter correction or half-hour reset cannot silently produce a trusted hourly baseline."""
    data = history()
    rows = data.hours[:2] + (replace(data.hours[2], total_kwh=99),) + data.hours[3:]
    with pytest.raises(ValueError, match="decrease"):
        replace(data, hours=rows).baseline(START, START + timedelta(hours=4))
    with pytest.raises(ValueError):
        data.baseline(START + timedelta(minutes=30), START + timedelta(hours=4))


def test_manual_seed_is_not_overwritten_and_versions_bind_configuration():
    """A physical archive remains readable but does not suppress annual statistics. Source/configuration changes yield a different read identity."""
    contract, config = inputs()
    service = BonusHistory()
    request = service.request(contract, config, None, NOW)
    manual = CounterBook(NOW, (CounterSeed("solar", START, 99, 5, "sensor.export", START),))
    assert service.request(contract, config, manual, NOW) is not None
    assert (
        service.request(contract, replace(config, revision="config-2"), None, NOW).version
        != request.version
    )
    moved = replace(
        config, bindings=(config.bindings[0], replace(config.bindings[1], entity_id="sensor.other"))
    )
    assert service.request(contract, moved, manual, NOW).entity_id == "sensor.other"
    with pytest.raises(ValueError, match="source"):
        service.derive(request, replace(history(), entity_id="sensor.other"), None, NOW)


@pytest.mark.parametrize("timezone", ["Europe/Amsterdam", "Europe/London"])
@pytest.mark.parametrize(
    "instant", [datetime(2026, 3, 30, 12, tzinfo=UTC), datetime(2026, 10, 26, 12, tzinfo=UTC)]
)
def test_local_year_boundary_is_absolute_across_dst(timezone, instant):
    """Use the local contractual January boundary. UTC elapsed hours remain continuous across DST."""
    contract, config = inputs()
    rule = replace(contract.bonuses[0], timezone=timezone)
    contract = replace(contract, bonuses=(rule,))
    request = BonusHistory().request(contract, config, None, instant)
    start = rule.period_start(instant)
    count = int((request.end - start).total_seconds() / 3600) + 1
    data = EnergyHistory(
        "sensor.export",
        tuple(
            EnergyHour(start + timedelta(hours=index_item - 1), index_item, index_item)
            for index_item in range(count)
        ),
    )
    assert data.baseline(start, request.end)[0] == count - 1


@pytest.mark.parametrize(
    "stamp, request_available",
    [
        ("2026-10-31T22:20:00+00:00", True),
        ("2026-10-31T23:20:00+00:00", False),
        ("2026-11-01T00:20:00+00:00", True),
    ],
)
def test_annual_reset_waits_for_safe_history_and_local_next_month_avoids_the_boundary(
    stamp, request_available
):
    """An Amsterdam November reset waits through its first hour. A fixture using the next local month always retains usable history."""
    contract, configuration = inputs()
    instant = datetime.fromisoformat(stamp)
    start = datetime(2026, 10, 29, tzinfo=UTC)
    rule = replace(
        contract.bonuses[0],
        effective_from=start,
        contract_start=start,
        timezone="Europe/Amsterdam",
        reset_month=11,
    )
    request = BonusHistory().request(
        replace(contract, bonuses=(rule,)), configuration, None, instant
    )
    assert (request is not None) is request_available
    next_month = instant.astimezone(ZoneInfo("Europe/Amsterdam")).month % 12 + 1
    future_reset = replace(rule, reset_month=next_month)
    assert (
        BonusHistory().request(
            replace(contract, bonuses=(future_reset,)), configuration, None, instant
        )
        is not None
    )


def test_wire_rejects_wrong_units_duplicates_and_oversized_history():
    """Require normalized kWh and unique ordered bounded rows at the app transport boundary."""
    body = {
        "request_version": "test",
        "entity_id": "sensor.export",
        "unit": "kWh",
        "hours": [[START.timestamp(), 1, 1]],
    }
    assert parse_history(body).hours[0].meter_kwh == 1
    for changed in ({"unit": "Wh"}, {"hours": body["hours"] * 2}, {"hours": body["hours"] * 9001}):
        with pytest.raises(ValueError):
            parse_history({**body, **changed})


@pytest.mark.parametrize("timestamp", [1e300, -1e300, float("inf"), float("nan"), 10**100, 1e12])
def test_wire_rejects_unrepresentable_timestamps_as_validation_errors(timestamp):
    """Extreme, nonfinite and out-of-range timestamps produce a stable validation error on every HTTP caller."""
    body = {
        "request_version": "test",
        "entity_id": "sensor.export",
        "unit": "kWh",
        "hours": [[timestamp, 1, 1]],
    }
    with pytest.raises(ValueError, match="Invalid history timestamp"):
        parse_history(body)


def test_additional_bonus_uses_its_own_contract_year_without_replacing_manual_seed():
    """A second rule counts only January 2 export. Its automatic 8 kWh seed leaves the first rule's confirmed 99 kWh intact."""
    contract, config = inputs()
    now = NOW + timedelta(days=1)
    second = replace(
        contract.bonuses[0], rule_id="extra", reset_day=2, effective_from=START + timedelta(days=1)
    )
    contract = replace(contract, bonuses=contract.bonuses + (second,))
    original = CounterSeed("solar", START, 99, 500, "sensor.export", START)
    manual = CounterBook(NOW, (original,))
    service = BonusHistory()
    request = service.request(contract, config, manual, now)
    assert request.periods == (("extra", START + timedelta(days=1)),)
    data = replace(
        history(),
        hours=tuple(
            replace(hour, start=hour.start + timedelta(days=1)) for hour in history().hours
        ),
    )
    book = service.derive(request, data, manual, now)
    assert book.seeds[0] is original
    assert book.seeds[1].used_kwh == 8
    assert book.seeds[1].period_start == START + timedelta(days=1)


def test_full_counter_book_can_roll_forward_without_deleting_archived_evidence(tmp_path):
    """Reconstruct current rules to 8 kWh while keeping all former physical seeds and their values in the immutable old archive."""
    from open_energy_planner.storage.counters import CounterStore

    contract, config = inputs()
    contract = replace(
        contract,
        bonuses=contract.bonuses
        + (
            replace(
                contract.bonuses[0], rule_id="keep", effective_from=START + timedelta(minutes=1)
            ),
            replace(contract.bonuses[0], effective_from=NOW + timedelta(minutes=15)),
        ),
    )
    retained = CounterSeed("keep", START, 99, 500, "sensor.export", START)
    old = CounterBook(
        NOW,
        tuple(
            CounterSeed(
                "removed",
                START.replace(year=1950 + index),
                index,
                index,
                "sensor.export",
                START.replace(year=1950 + index),
            )
            for index in range(63)
        )
        + (retained,),
    )
    store = make_store(tmp_path, CounterStore)
    store.save(old)
    archive = f"{old.version}.json"
    original = read_document(tmp_path, "bonus-counters", archive)
    service = BonusHistory()
    request = service.request(contract, config, old, NOW)
    new = service.derive(request, history(), old, NOW)
    assert len(new.seeds) == 2
    assert new.seeds[0].rule_id == "keep"
    assert new.seeds[0].used_kwh == 8
    assert new.seeds[1].used_kwh == 8
    store.save(new)
    assert read_document(tmp_path, "bonus-counters", archive) == original
    assert store.load(old.version) == old


def test_draft_history_cache_expires_and_reset_changes_reject_delayed_reports():
    """Preview polling keeps a request stable, a changed reset replaces it, and an abandoned draft stops requesting after five minutes."""
    from open_energy_planner.contracts.bonus_history import BonusBudgetPreview

    contract, config = inputs()
    preview = BonusBudgetPreview()
    preview.update(contract, NOW)
    first = preview.request(config, None, NOW)
    preview.update(
        replace(contract, recorded_at=NOW + timedelta(seconds=1)), NOW + timedelta(seconds=1)
    )
    assert preview.request(config, None, NOW + timedelta(seconds=1)).version == first.version
    changed = replace(contract, bonuses=(replace(contract.bonuses[0], reset_month=12),))
    preview.update(changed, NOW)
    assert preview.request(config, None, NOW).version != first.version
    assert preview.request(config, None, NOW + timedelta(minutes=6)) is None


def test_abandoned_draft_drops_temporary_baseline_on_next_preview():
    """An idle draft cannot resurrect its five-minute-expired inferred baseline when the user returns."""
    from open_energy_planner.contracts.bonus_history import BonusBudgetPreview

    contract, config = inputs()
    preview = BonusBudgetPreview()
    preview.update(contract, NOW)
    pending = preview.request(config, None, NOW)
    from open_energy_planner.contracts.bonus_statistics import parse_statistics
    from tests.app.history.test_bonus_statistics import report

    preview.receive(pending, parse_statistics(report(pending), pending), None, NOW)
    assert preview.book.seeds[0].used_kwh == 8
    preview.update(contract, NOW + timedelta(minutes=6))
    assert preview.book is None
    assert preview.request(config, None, NOW + timedelta(minutes=6)) is not None
