"""Annual sums, immutable missing-prefix references and safe refresh/restart behaviour."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.contracts.bonus_history import BonusHistory
from open_energy_planner.contracts.bonus_statistics import derive_statistics, parse_statistics
from open_energy_planner.contracts.compilation import BonusCompiler
from open_energy_planner.storage.counters import CounterStore
from tests.app.history.support.bonus import NOW, START, inputs
from tests.database.support.archive import make_store


def report(request, *, offset=0, meters=("sensor.export",), state=True):
    """Build independent 2-kWh hourly sums with a physical reset in the final hour."""
    return {
        "request_version": request.version,
        "statistics": [
            {
                "statistic_id": entity,
                "metadata_id": index + 1,
                "source": "recorder",
                "unit": "kWh",
                "hours": [
                    [
                        (START + timedelta(hours=hour - 1)).timestamp(),
                        100 + 2 * hour,
                        (500 + hour if hour < 4 else 1) if state else None,
                    ]
                    for hour in range(offset, 5)
                ],
            }
            for index, entity in enumerate(meters)
        ],
    }


def derive(body=None, *, book=None, now=NOW):
    """Run the same strict parsing and arithmetic used by the authenticated history route."""
    contract, config = inputs()
    request = BonusHistory().request(contract, config, book, now)
    if request is None:
        request = BonusHistory().request(contract, config, None, now)
    body = body or report(request)
    return derive_statistics(request, parse_statistics(body, request), book, now)


def test_complete_sum_history_counts_resets_without_live_physical_reading():
    """100 to 108 sum means 8 kWh even when physical state resets from 503 to 1."""
    book = derive()
    seed = book.seeds[0]
    assert seed.statistics.reference_at == START
    assert seed.statistics.prefix_kwh == 0
    assert seed.statistics.used(NOW) == 8
    contract, config = inputs()
    evidence = BonusCompiler().current_evidence(contract, NOW, book, config, None)[0]
    assert evidence.initial_kwh == 8
    assert evidence.meter_kwh == 108
    assert BonusHistory().request(contract, config, book, NOW) is None


def test_missing_prefix_uses_first_endpoint_and_adds_only_subsequent_sum():
    """First usable sum 104 ends at 02:00. A 3500-kWh prefix plus 108-104 gives 3504."""
    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, NOW)
    pending = derive(report(request, offset=2))
    basis = pending.seeds[0].statistics
    assert pending.seeds[0].used_kwh is None
    with pytest.raises(ValueError, match="must be null"):
        replace(pending.seeds[0], used_kwh=0)
    assert basis.reference_at == START + timedelta(hours=2)
    with pytest.raises(ValueError, match="prefix_required"):
        basis.used(NOW)
    confirmed = replace(basis, prefix_kwh=3500, confirmed_at=NOW)
    assert confirmed.used(NOW) == 3504
    assert confirmed.reference_version == basis.reference_version
    assert confirmed.series[0].reference_sum == 104


@pytest.mark.parametrize("offset", [0, 2])
def test_recorder_roundoff_keeps_raw_evidence_and_nonnegative_usage(offset):
    """A measured tiny negative drift permits a reference without inventing export."""
    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, NOW)
    body = report(request, offset=offset, state=False)
    for index, row in enumerate(body["statistics"][0]["hours"]):
        row[1] = 0 if index == 0 else -1.225398591486737e-7
    book = derive(body)
    basis = book.seeds[0].statistics
    assert basis.series[0].reference_sum == 0
    assert basis.series[0].latest_sum == -1.225398591486737e-7
    confirmed = replace(basis, prefix_kwh=25, confirmed_at=NOW)
    assert confirmed.used(NOW) == 25
    assert basis.prefix_kwh == (0 if offset == 0 else None)


def test_intermediate_decreases_do_not_change_endpoint_arithmetic():
    """Intervening corrections do not replace the first-to-last one-kWh difference."""
    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, NOW)
    body = report(request, state=False)
    for index, row in enumerate(body["statistics"][0]["hours"]):
        row[1] = 10 - index * 0.0000004
    body["statistics"][0]["hours"][-1][1] = 11
    assert derive(body).seeds[0].statistics.used(NOW) == 1


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("dipped_sum_kwh", [3.999999, 3.999998999])
def test_negative_period_difference_is_zero_until_export_recovers(recovery, dipped_sum_kwh):
    """Negative differences contribute zero. Recovery from reference 4 to latest 4.3 gives 0.3 kWh."""
    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, NOW)
    body = report(request, state=False)
    hourly_sums_kwh = (
        [4, dipped_sum_kwh, 4.1, 4.2, 4.3]
        if recovery
        else [4, dipped_sum_kwh, dipped_sum_kwh, dipped_sum_kwh, dipped_sum_kwh]
    )
    for row, sum_kwh in zip(body["statistics"][0]["hours"], hourly_sums_kwh, strict=True):
        row[1] = sum_kwh
    basis = derive(body).seeds[0].statistics
    assert basis.used(NOW) == pytest.approx(0.3 if recovery else 0)


def test_first_available_endpoint_accepts_prefix_then_counts_only_measured_export():
    """One retained endpoint anchors 3500 confirmed kWh, then one new hour adds two."""
    contract, config = inputs()
    service = BonusHistory()
    request = service.request(contract, config, None, NOW)
    body = report(request, offset=4)
    pending = derive(body)
    seed = pending.seeds[0]
    assert seed.statistics.reference_at == request.end
    assert seed.statistics.prefix_kwh is None
    basis = replace(seed.statistics, prefix_kwh=3500, confirmed_at=NOW)
    confirmed = replace(pending, seeds=(replace(seed, used_kwh=3500, statistics=basis),))
    assert basis.used(NOW) == 3500
    later = NOW + timedelta(hours=1)
    request = service.request(contract, config, confirmed, later)
    body["request_version"] = request.version
    body["statistics"][0]["hours"].append([(START + timedelta(hours=4)).timestamp(), 110, 3])
    refreshed = derive_statistics(request, parse_statistics(body, request), confirmed, later)
    assert refreshed.seeds[0].statistics.used(later) == 3502
    assert refreshed.seeds[0].statistics.reference_version == basis.reference_version


def test_restart_keeps_reference_prefix_and_provenance(tmp_path):
    """Decode persisted SQL JSON and reproduce the same 8-kWh total after restart."""
    store = make_store(tmp_path, CounterStore)
    book = derive()
    store.save_statistics(book)
    loaded = store.load(book.version)
    assert loaded == book
    assert loaded.seeds[0].statistics.used(NOW) == 8
    assert store.provenance(loaded.seeds[0])["statistics"]["series"][0]["reference_sum"] == 100


def test_hourly_refresh_preserves_reference_and_verifies_the_retained_checkpoint():
    """One new 2-kWh hour raises the budget to 10 while reference identity stays fixed."""
    book = derive()
    contract, config = inputs()
    now = NOW + timedelta(hours=1)
    request = BonusHistory().request(contract, config, book, now)
    body = report(request)
    body["statistics"][0]["hours"].append([(START + timedelta(hours=4)).timestamp(), 110, 3])
    new = derive_statistics(request, parse_statistics(body, request), book, now)
    assert new.seeds[0].statistics.used(now) == 10
    assert new.seeds[0].statistics.reference_version == book.seeds[0].statistics.reference_version


@pytest.mark.parametrize(
    "change", ["anchor", "identity", "unit", "missing_reference", "missing_latest"]
)
def test_history_changes_block_the_counter_without_replacing_its_reference(change):
    """Changed references, metadata and missing endpoints cannot replace retained evidence."""
    book = derive()
    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, NOW)
    body = report(request)
    channel = body["statistics"][0]
    if change == "anchor":
        channel["hours"][0][1] = 99
    elif change == "missing_reference":
        del channel["hours"][0]
    elif change == "missing_latest":
        del channel["hours"][-1]
    elif change == "identity":
        channel["metadata_id"] = 99
    elif change == "unit":
        channel["unit"] = "Wh"
    result = derive_statistics(request, parse_statistics(body, request), book, NOW)
    basis = result.seeds[0].statistics
    assert basis.reference_version == book.seeds[0].statistics.reference_version
    assert basis.history_digest == book.seeds[0].statistics.history_digest
    assert basis.rejected_digest
    with pytest.raises(ValueError, match="revalidation_required"):
        basis.used(NOW)
    recovered = derive(book=result)
    assert recovered.seeds[0].statistics.used(NOW) == 8


def test_sum_only_series_needs_no_physical_state_and_expires():
    """Recorder sum remains usable without state. Stale sums cannot authorize a current budget."""
    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, NOW)
    book = derive(report(request, state=False))
    assert book.seeds[0].statistics.used(NOW) == 8
    assert not book.seeds[0].statistics.physical_available
    assert book.seeds[0].meter_kwh is None
    with pytest.raises(ValueError, match="must be null"):
        replace(book.seeds[0], meter_kwh=0)
    with pytest.raises(ValueError, match="expired"):
        book.seeds[0].statistics.used(NOW + timedelta(hours=4))


def test_multi_meter_common_reference_and_independent_endpoint_differences():
    """Two 4-kWh deltas share a late reference. An interior correction has no effect."""
    contract, config = inputs()
    config = replace(
        config,
        bindings=config.bindings
        + (
            replace(
                config.bindings[-1], key="grid.export_energy.second", entity_id="sensor.second"
            ),
        ),
    )
    request = BonusHistory().request(contract, config, None, NOW)
    body = report(request, meters=("sensor.export", "sensor.second"))
    body["statistics"][1]["hours"] = body["statistics"][1]["hours"][2:]
    book = derive_statistics(request, parse_statistics(body, request), None, NOW)
    basis = replace(book.seeds[0].statistics, prefix_kwh=10, confirmed_at=NOW)
    assert basis.reference_at == START + timedelta(hours=2)
    assert basis.used(NOW) == 18
    broken = deepcopy(body)
    broken["statistics"][0]["hours"][3][1] = 103
    corrected = derive_statistics(request, parse_statistics(broken, request), None, NOW)
    assert corrected.seeds[0].statistics.series == book.seeds[0].statistics.series
    mixed = deepcopy(body)
    mixed["statistics"][0]["hours"][-1][1] = 102
    mixed_book = derive_statistics(request, parse_statistics(mixed, request), None, NOW)
    mixed_basis = replace(mixed_book.seeds[0].statistics, prefix_kwh=10, confirmed_at=NOW)
    # Reference 104 to latest 102 contributes zero. The other meter contributes four.
    assert mixed_basis.used(NOW) == 14


@pytest.mark.parametrize("meters", [("sensor.export",), ("sensor.export", "sensor.second")])
def test_channel_without_a_reference_for_a_later_period_is_incomplete(meters):
    """Rows valid for an older requested period cannot initialize a newer period.

    One meter stops before the newer period's opening hour. No zero reference or
    HTTP-server exception may replace the expected incomplete-history rejection.
    """
    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, NOW)
    periods = (("solar", START + timedelta(hours=2)), ("older", START))
    request = replace(request, periods=periods, active_periods=periods, entity_ids=meters)
    body = report(request, meters=meters)
    body["statistics"][-1]["hours"] = body["statistics"][-1]["hours"][:2]
    channels = parse_statistics(body, request)
    with pytest.raises(ValueError, match="Incomplete export statistics"):
        derive_statistics(request, channels, None, NOW)


def test_new_annual_period_gets_an_independent_zero_prefix(tmp_path):
    """The next January uses its own 200-to-208 sums and leaves last year's archive intact."""
    book = derive()
    contract, config = inputs()
    next_year = NOW.replace(year=2027)
    request = BonusHistory().request(contract, config, book, next_year)
    body = report(request)
    for row in body["statistics"][0]["hours"]:
        instant = datetime.fromtimestamp(row[0], UTC)
        row[0] = instant.replace(year=instant.year + 1).timestamp()
        row[1] += 100
    result = derive_statistics(request, parse_statistics(body, request), book, next_year)
    assert len(result.seeds) == 1
    assert result.seeds[0].statistics.reference_at == START.replace(year=2027)
    assert result.seeds[0].statistics.prefix_kwh == 0
    assert result.seeds[0].statistics.used(next_year) == 8
    store = make_store(tmp_path, CounterStore)
    store.save_statistics(book)
    store.save_statistics(result)
    assert store.load(book.version).seeds[0].statistics.used(NOW) == 8


def test_manual_prefix_survives_checkpoint_refresh_with_negative_absolute_sums():
    """Signed sums are legitimate. A 25-kWh prefix plus a 6-kWh delta remains 31 after refresh."""
    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, NOW)
    body = report(request, offset=2, state=False)
    for row in body["statistics"][0]["hours"]:
        row[1] -= 200
    book = derive(body)
    seed = book.seeds[0]
    confirmed = replace(seed.statistics, prefix_kwh=25, confirmed_at=NOW)
    book = replace(book, seeds=(replace(seed, used_kwh=29, statistics=confirmed),))
    now = NOW + timedelta(hours=1)
    request = BonusHistory().request(contract, config, book, now)
    body["request_version"] = request.version
    body["statistics"][0]["hours"].append([(START + timedelta(hours=4)).timestamp(), -90, None])
    result = derive_statistics(request, parse_statistics(body, request), book, now)
    assert result.seeds[0].statistics.used(now) == 31
    assert result.seeds[0].statistics.confirmed_at == NOW


def test_repeated_rejected_history_keeps_counter_and_request_versions_stable():
    """An identical rejection does not bypass the integration's retry delay through new book versions."""
    from open_energy_planner.contracts.bonus_statistics import invalidate_statistics

    book = derive()
    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, NOW)
    body = report(request)
    body["statistics"][0]["hours"][0][1] -= 1
    channels = parse_statistics(body, request)
    rejected = derive_statistics(request, channels, book, NOW)
    repeated = derive_statistics(request, channels, rejected, NOW + timedelta(seconds=30))
    assert repeated is rejected
    assert (
        BonusHistory().request(contract, config, repeated, NOW).version
        == BonusHistory().request(contract, config, rejected, NOW).version
    )
    malformed = {"request_version": "first", "statistics": "invalid"}
    invalid = invalidate_statistics(book, request, malformed, NOW)
    repeated = invalidate_statistics(
        invalid, request, malformed | {"request_version": "second"}, NOW + timedelta(seconds=30)
    )
    assert repeated is invalid


@pytest.mark.parametrize("prefix", [0, 25])
def test_correction_and_recovery_keep_the_reference_and_confirmed_prefix(prefix):
    """100 to 150 gives 50, correction to 149 gives 49, then 99 gives zero and 101 gives one."""
    contract, config = inputs()
    now = NOW
    request = BonusHistory().request(contract, config, None, now)
    body = report(request, state=False)
    rows = body["statistics"][0]["hours"]
    rows[:] = [rows[0], rows[-1]]
    rows[-1][1] = 150
    book = derive(body)
    seed = book.seeds[0]
    basis = replace(seed.statistics, prefix_kwh=prefix, confirmed_at=NOW)
    book = replace(book, seeds=(replace(seed, statistics=basis, used_kwh=prefix + 50),))
    for latest_sum, expected in [(149, 49), (99, 0), (101, 1)]:
        now += timedelta(hours=1)
        request = BonusHistory().request(contract, config, book, now)
        # Only the fixed reference and the requested latest endpoint are needed.
        rows[-1] = [(request.end - timedelta(hours=1)).timestamp(), latest_sum, None]
        body["request_version"] = request.version
        book = derive_statistics(request, parse_statistics(body, request), book, now)
        assert book.seeds[0].used_kwh == prefix + expected
        assert book.seeds[0].statistics.reference_version == basis.reference_version
        assert book.seeds[0].statistics.confirmed_at == NOW


def test_annual_boundary_alone_establishes_zero_without_delaying_another_hour():
    """The compiled 23:00 row ends at January 1 and supplies a zero-delta annual reference."""
    contract, config = inputs()
    now = START + timedelta(hours=1, minutes=30)
    request = BonusHistory().request(contract, config, None, now)
    body = report(request)
    body["statistics"][0]["hours"] = body["statistics"][0]["hours"][:1]
    book = derive_statistics(request, parse_statistics(body, request), None, now)
    assert book.seeds[0].statistics.reference_at == START
    assert book.seeds[0].statistics.used(now) == 0


def test_annual_requests_do_not_change_the_historical_settlement_wire_format():
    """Only the annual request selects the sum protocol. Historical settlement still receives hourly physical endpoints."""
    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, NOW)
    assert request.document()["basis"] == "statistics_sum"
    assert "basis" not in replace(request, statistics_sum=False).document()


def test_physical_archive_cannot_authorize_or_suppress_annual_statistics():
    """A former 999-kWh physical seed stays in archives but annual accounting requires the real 8-kWh sum evidence."""
    from open_energy_planner.contracts.bonus import CounterBook, CounterSeed

    contract, config = inputs()
    physical = CounterBook(NOW, (CounterSeed("solar", START, 999, 50, "sensor.export", START),))
    request = BonusHistory().request(contract, config, physical, NOW)
    assert request is not None
    with pytest.raises(ValueError, match="baseline_required"):
        BonusCompiler().current_evidence(contract, NOW, physical, config, None)
    book = derive_statistics(request, parse_statistics(report(request), request), physical, NOW)
    assert book.seeds[0].statistics.used(NOW) == 8


@pytest.mark.parametrize("error", ["history_missing", "unsupported_statistics"])
def test_missing_source_invalidates_an_unconfirmed_prefix_without_inventing_usage(error):
    """A disappeared source blocks an unknown prefix and preserves its exact reference for recovery."""
    from open_energy_planner.contracts.bonus_statistics import invalidate_statistics

    contract, config = inputs()
    request = BonusHistory().request(contract, config, None, NOW)
    pending = derive(report(request, offset=2))
    invalid = invalidate_statistics(
        pending, request, {"request_version": request.version, "error": error}, NOW
    )
    seed = invalid.seeds[0]
    assert seed.used_kwh is None
    assert seed.statistics.prefix_kwh is None
    assert seed.statistics.reference_version == pending.seeds[0].statistics.reference_version
    with pytest.raises(ValueError, match="revalidation_required"):
        seed.statistics.used(NOW)
