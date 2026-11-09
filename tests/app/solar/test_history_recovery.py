"""Recover bounded timestamp rounding without manufacturing solar production measurements."""

from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.solar.storage.history import parse_report

START = datetime(2025, 6, 1, 10, tzinfo=UTC)


def report(rows, entity="sensor.synthetic_production", *, unit="kWh"):
    """Supply an independent synthetic Recorder envelope with original cumulative endpoints."""
    return parse_report(
        {
            "basis": "solar_history_chunk",
            "request_version": "synthetic-request",
            "entity_id": entity,
            "statistics_identity": {"metadata_id": 7, "source": "recorder", "unit": "kWh"},
            "unit": unit,
            "hours": rows,
        }
    )


@pytest.mark.parametrize("offset", [-64, -16, 0, 16, 64])
def test_small_offsets_use_nearest_hour_and_preserve_original_endpoint(offset):
    """Small negative offsets stay in the intended next hour, with exact raw values retained."""
    raw = [
        [START.timestamp(), 100.0, 80.0],
        [(START + timedelta(hours=1, seconds=offset)).timestamp(), 102.0, 82.0],
        [(START + timedelta(hours=2)).timestamp(), 103.0, 83.0],
    ]
    accepted = report(raw)
    assert [(hour.start, hour.total_kwh) for hour in accepted.history.hours] == [
        (START, 100.0),
        (START + timedelta(hours=1), 102.0),
        (START + timedelta(hours=2), 103.0),
    ]
    assert accepted.raw_hours == tuple(tuple(row) for row in raw)


def test_bad_endpoints_do_not_reject_good_rows_or_invent_zero_production():
    """Unplaceable, missing and malformed points leave gaps inside otherwise usable history."""
    raw = [
        [START.timestamp(), 100.0, 80.0],
        [(START + timedelta(hours=1, seconds=65)).timestamp(), 101.0, 81.0],
        [(START + timedelta(hours=2)).timestamp(), None, None],
        [None, 103.0, 83.0],
        [(START + timedelta(hours=4)).timestamp(), 112.0, 92.0],
        [(START + timedelta(hours=5)).timestamp(), 115.0, 95.0],
    ]
    accepted = report(raw)
    assert [(hour.start, hour.total_kwh) for hour in accepted.history.hours] == [
        (START, 100.0),
        (START + timedelta(hours=4), 112.0),
        (START + timedelta(hours=5), 115.0),
    ]
    assert accepted.raw_hours == tuple(tuple(row) for row in raw)
    assert accepted.history.hours[-1].total_kwh - accepted.history.hours[0].total_kwh == 15.0


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_duplicate_hour_is_excluded_without_choosing_a_winner(reverse):
    """Two different endpoints for one rounded hour exclude only that endpoint regardless of order."""
    conflict = (START + timedelta(hours=1)).timestamp()
    raw = [
        [START.timestamp(), 100.0, 80.0],
        [conflict, 102.0, 82.0],
        [conflict + 16, 103.0, 83.0],
        [(START + timedelta(hours=2)).timestamp(), 104.0, 84.0],
        [(START + timedelta(hours=3)).timestamp(), 105.0, 85.0],
    ]
    if reverse:
        raw.reverse()
    accepted = report(raw)
    assert [hour.start for hour in accepted.history.hours] == [
        START,
        START + timedelta(hours=2),
        START + timedelta(hours=3),
    ]
    assert accepted.excluded_starts == frozenset({START + timedelta(hours=1)})
    assert accepted.raw_hours == tuple(tuple(row) for row in raw)


def test_matching_duplicate_readings_keep_one_hour_and_both_original_timestamps():
    """Identical duplicate measurements carry no extra training weight and retain original evidence."""
    raw = [
        [START.timestamp(), 100.0, 80.0],
        [START.timestamp() + 16, 100.0, 80.0],
        [(START + timedelta(hours=1)).timestamp(), 101.0, 81.0],
    ]
    accepted = report(raw)
    assert len(accepted.history.hours) == 2
    assert accepted.raw_hours == tuple(tuple(row) for row in raw)


def test_all_unusable_rows_complete_an_empty_chunk_without_fake_measurements():
    """An unusable portion can finish without manufacturing a zero row or blocking later years."""
    accepted = report([[START.timestamp(), None, None], [None, 100.0, 80.0]])
    assert accepted.history is None
    assert accepted.raw_hours == ((START.timestamp(), None, None), (None, 100.0, 80.0))


@pytest.mark.parametrize("meter", [0, None, -1, float("nan")])
def test_reset_or_unavailable_physical_state_does_not_change_sum_targets(meter):
    """State is optional evidence, never the production target or a gate for a valid sum."""
    accepted = report(
        [[START.timestamp(), 100, 987], [(START + timedelta(hours=1)).timestamp(), 103, meter]]
    )
    previous, following = accepted.history.hours
    assert following.total_kwh - previous.total_kwh == 3


def test_equal_sum_with_different_physical_states_is_not_a_sum_conflict():
    """Recorder state changes cannot turn matching cumulative sums into an ambiguous target."""
    accepted = report([[START.timestamp(), 100, 987], [START.timestamp() + 16, 100, 0]])
    assert accepted.excluded_starts == frozenset()
    assert [(hour.start, hour.total_kwh) for hour in accepted.history.hours] == [(START, 100)]


def test_learner_skips_the_accumulated_gap_jump_but_uses_the_next_contiguous_hour():
    """A 12 kWh jump across three hours is not hourly truth. The next 3 kWh hour is usable."""
    from open_energy_planner.solar.calculation.model import SolarEnergy
    from open_energy_planner.solar.learning import training_sample_issue
    from tests.app.solar.support import plant

    configuration = plant()
    accepted = report(
        [
            [(START - timedelta(hours=1)).timestamp(), 100, 100],
            [(START + timedelta(hours=2)).timestamp(), 112, 0],
            [(START + timedelta(hours=3)).timestamp(), 115, None],
        ]
    )
    before_gap, after_gap, next_hour = accepted.history.hours
    expected = SolarEnergy("pv0", after_gap.start, 1, 3.125, 3)
    before = START + timedelta(days=1)
    assert (
        training_sample_issue(
            configuration.sources[0],
            configuration.inverters[0],
            before_gap,
            after_gap,
            expected,
            before,
        )
        == "history_gap"
    )
    assert (
        training_sample_issue(
            configuration.sources[0],
            configuration.inverters[0],
            after_gap,
            next_hour,
            expected,
            before,
        )
        is None
    )
    assert next_hour.total_kwh - after_gap.total_kwh == 3
    assert next_hour.total_kwh - before_gap.total_kwh == 15


def test_sum_decrease_remains_a_meter_correction_even_if_state_increases():
    """Sum accounts for resets but edited Recorder sums must still fail interval admission safely."""
    from open_energy_planner.solar.calculation.model import SolarEnergy
    from open_energy_planner.solar.learning import training_sample_issue
    from tests.app.solar.support import plant

    configuration = plant()
    accepted = report(
        [[START.timestamp(), 100, 5], [(START + timedelta(hours=1)).timestamp(), 99, 6]]
    )
    previous, following = accepted.history.hours
    assert (
        training_sample_issue(
            configuration.sources[0],
            configuration.inverters[0],
            previous,
            following,
            SolarEnergy("pv0", following.start, 1, 1, 0.96),
            START + timedelta(days=1),
        )
        == "meter_correction"
    )


def test_retracted_sum_withdraws_stale_models_and_empty_history_does_not_survive(tmp_path):
    """Removing previously accepted endpoints revokes queued workers and live ML without losing raw evidence."""
    from open_energy_planner.solar.runtime import SolarRuntime
    from open_energy_planner.solar.storage import SolarStore
    from tests.app.solar.history_support import HISTORY_NOW, discover, runtime_case
    from tests.app.solar.support import SITE, weather

    runtime = runtime_case(tmp_path)
    discover(runtime, first="2026-01-01T00:00:00+00:00")
    pending = runtime.request(HISTORY_NOW)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    original = report(
        [[start.timestamp(), 100, 99], [(start + timedelta(hours=1)).timestamp(), 103, 0]],
        entity="sensor.pv0",
    )
    runtime.receive(pending["version"], original.history, HISTORY_NOW, report=original)
    runtime.set_weather(SITE, weather(HISTORY_NOW))
    queued = runtime.job(HISTORY_NOW)
    runtime.candidate = object()
    runtime.calibrations = (object(),)
    runtime.source_selection = {"pv0": {"active": "ml"}}
    now = HISTORY_NOW + timedelta(days=1)
    pending = runtime.request(now)
    missing = report([[start.timestamp(), None, 99]], entity="sensor.pv0")
    runtime.receive(pending["version"], None, now, report=missing)
    assert runtime.histories == {}
    assert runtime.candidate is None
    assert runtime.calibrations == ()
    assert runtime.source_selection == {}
    assert not runtime.install((queued[0],), now)
    restored = SolarRuntime(SolarStore(runtime.store.documents))
    assert restored.histories == {}
    assert restored.history_issues == {}
    assert restored.history_import.states["sensor.pv0"]["conflicts"] == [
        [start.timestamp(), 100],
        [(start + timedelta(hours=1)).timestamp(), 103],
    ]
    now += timedelta(days=1)
    pending = restored.request(now)
    restored.receive(pending["version"], original.history, now, report=original)
    assert restored.histories["sensor.pv0"] == original.history


@pytest.mark.parametrize("offset", [-64, 64])
def test_midnight_alignment_retains_the_correct_utc_date(offset):
    """Year and request boundaries use nearest-hour placement, never truncation to the previous day."""
    boundary = datetime(2026, 1, 1, tzinfo=UTC)
    accepted = report([[boundary.timestamp() + offset, 100, None]])
    assert accepted.history.hours[0].start == boundary


def test_oversized_transfers_and_wrong_units_still_fail_at_the_envelope():
    """Recoverable rows do not weaken the independent transport-size or energy-unit boundary."""
    with pytest.raises(ValueError, match="history size"):
        report([[START.timestamp(), 100, None]] * 9001)
    with pytest.raises(ValueError, match="Unsupported solar history unit"):
        report([[START.timestamp(), 100, None]], unit="Wh")
