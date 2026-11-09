"""Import all available solar years without expanding a Recorder transfer or inventing gaps."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.history.wire import parse_history
from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.storage.history import parse_report
from tests.app.solar.history_support import HISTORY_IDENTITY as IDENTITY
from tests.app.solar.history_support import HISTORY_NOW as NOW
from tests.app.solar.history_support import chunk_report, discover, finish, runtime_case
from tests.app.solar.support import plant
from tests.database.support.archive import make_store


def test_solar_discovers_oldest_statistics_before_requesting_bounded_years(tmp_path):
    """The first source requests monthly availability instead of silently limiting training to one year."""
    store = make_store(tmp_path, SolarStore)
    store.save(plant())
    runtime = SolarRuntime(store)
    request = runtime.request(datetime(2026, 11, 8, 12, tzinfo=UTC))
    assert request["basis"] == "solar_history_range"
    assert request["start"] == "1970-01-01T00:00:00+00:00"
    assert request["entity_id"] == "sensor.pv0"


def test_multiple_years_resume_across_restart_and_refresh_only_the_current_year(tmp_path):
    """Three closed years and one live year merge once, retain an interior gap and restart without refetching."""
    runtime = runtime_case(tmp_path)
    discover(runtime)
    missing = datetime(2024, 7, 2, 12, tzinfo=UTC)
    requests = finish(runtime, gaps=(missing,))
    assert [request["import_year"] for request in requests] == [2023, 2024, 2025, 2026]
    for request in requests:
        start, end = (datetime.fromisoformat(request[key]) for key in ("start", "end"))
        assert end - start <= timedelta(days=367)
        assert (end - start) / timedelta(hours=1) <= 9000
    history = runtime.histories["sensor.pv0"]
    starts = [hour.start for hour in history.hours]
    assert len(starts) > 26000
    assert len(starts) == len(set(starts))
    assert missing not in starts
    assert (
        len(starts)
        == int(
            (
                datetime.fromisoformat(requests[-1]["end"])
                - datetime.fromisoformat(requests[0]["start"])
            )
            / timedelta(hours=1)
        )
        - 1
    )
    states = runtime.history_import.states["sensor.pv0"]
    closed = [chunk for chunk in states["chunks"] if chunk["year"] < 2026]
    restored = SolarRuntime(SolarStore(runtime.store.documents))
    assert restored.histories == runtime.histories
    assert restored.request(NOW) is None
    next_day = NOW + timedelta(days=1)
    pending = restored.request(next_day)
    assert pending["import_year"] == 2026
    report = chunk_report(pending)
    restored.receive(pending["version"], report.history, next_day, report=report)
    assert restored.request(next_day) is None
    assert [
        chunk
        for chunk in restored.history_import.states["sensor.pv0"]["chunks"]
        if chunk["year"] < 2026
    ] == closed
    assert len(restored.histories["sensor.pv0"].hours) == len(history.hours) + 24


def test_an_empty_interior_year_does_not_truncate_later_available_years(tmp_path):
    """An explicitly completed empty year leaves a gap while later annual requests still import."""
    runtime = runtime_case(tmp_path)
    discover(runtime)
    requests = finish(runtime, empty_years=(2024,))
    assert [request["import_year"] for request in requests] == [2023, 2024, 2025, 2026]
    hours = runtime.histories["sensor.pv0"].hours
    assert not any(hour.start.year == 2024 and hour.start.month == 6 for hour in hours)
    assert any(hour.start.year == 2025 for hour in hours)
    assert runtime.request(NOW) is None
    restored = SolarRuntime(SolarStore(runtime.store.documents))
    assert restored.histories == runtime.histories
    assert restored.request(NOW) is None


def test_one_failed_meter_advances_other_meters_and_retries_after_its_delay(tmp_path):
    """A failed first discovery cannot starve a second source or retry on every synchronization."""
    runtime = runtime_case(tmp_path, count=2)
    first = runtime.request(NOW)
    runtime.receive(first["version"], None, NOW, error="history_unavailable")
    second = runtime.request(NOW)
    assert second["entity_id"] == "sensor.pv1"
    discover(runtime, first=None)
    assert runtime.request(NOW + timedelta(minutes=14)) is None
    assert runtime.request(NOW + timedelta(minutes=15))["entity_id"] == "sensor.pv0"


def test_configuration_change_rejects_an_old_reply_without_discarding_cached_meter_years(tmp_path):
    """Geometry changes invalidate pending requests while retaining verified history for the same meter."""
    runtime = runtime_case(tmp_path)
    pending = runtime.request(NOW)
    source = replace(runtime.configuration.sources[0], tilt=45)
    configuration = replace(runtime.configuration, sources=(source,))
    runtime.store.save(configuration)
    runtime.configure(configuration)
    report = parse_report(
        {
            "basis": "solar_history_range",
            "request_version": pending["version"],
            "entity_id": pending["entity_id"],
            "statistics_identity": IDENTITY,
            "available_start": "2023-01-01T00:00:00+00:00",
        }
    )
    with pytest.raises(ValueError, match="request changed"):
        runtime.receive(pending["version"], None, NOW, report=report)
    discover(runtime)
    finish(runtime)
    accepted = runtime.histories.copy()
    configuration = replace(
        configuration,
        sources=(replace(source, tilt=25),),
    )
    runtime.store.save(configuration)
    runtime.configure(configuration)
    assert runtime.histories == accepted
    assert runtime.request(NOW) is None


def test_explicit_learning_date_opens_a_new_meter_cohort_and_preserves_original_quarantine(
    tmp_path,
    monkeypatch,
):
    """A deliberate post-replacement start binds new identity while date removal restores the original blocked cohort."""
    from open_energy_planner.solar.calculation.model import version

    runtime = runtime_case(tmp_path, count=2)
    for _ in range(2):
        discover(runtime, first="2025-01-01T00:00:00+00:00")
        for _ in range(2):
            pending = runtime.request(NOW)
            report = chunk_report(pending)
            runtime.receive(pending["version"], report.history, NOW, report=report)
    original_histories = runtime.histories.copy()
    original_chunks = list(runtime.history_import.states["sensor.pv0"]["chunks"])
    documents = runtime.store.documents
    default_pointer = "history-import-current-" + version("sensor.pv0") + ".json"
    default_reference = documents.read(default_pointer)
    original_manifest = documents.read("history-import-" + default_reference + ".json")
    next_day = NOW + timedelta(days=1)
    new_identity = {**IDENTITY, "metadata_id": 99}
    pending = runtime.request(next_day)
    conflict = chunk_report(pending, identity=new_identity)
    runtime.receive(pending["version"], conflict.history, next_day, report=conflict)
    assert runtime.history_issues["sensor.pv0"] == "history_continuity_unavailable"
    old_quarantine = "history-import-quarantine-" + version("sensor.pv0") + ".json"
    old_fault = documents.read(old_quarantine)
    configuration = replace(
        runtime.configuration,
        sources=(
            replace(runtime.configuration.sources[0], learning_start="2026-01-05T00:00:00+00:00"),
            runtime.configuration.sources[1],
        ),
    )
    runtime.store.save(configuration)
    runtime.configure(configuration)
    assert "sensor.pv0" not in runtime.histories
    assert "sensor.pv0" not in runtime.history_issues
    assert runtime.histories["sensor.pv1"] == original_histories["sensor.pv1"]
    pending = runtime.request(next_day)
    assert pending["basis"] == "solar_history_range"
    assert pending["start"] == "1970-01-01T00:00:00+00:00"
    report = parse_report(
        {
            "basis": "solar_history_range",
            "request_version": pending["version"],
            "entity_id": "sensor.pv0",
            "statistics_identity": new_identity,
            "available_start": "2023-01-01T00:00:00+00:00",
        }
    )
    runtime.receive(pending["version"], None, next_day, report=report)
    pending = runtime.request(next_day)
    assert pending["basis"] == "solar_history_chunk"
    assert pending["import_year"] == 2026
    assert pending["start"] == "2026-01-04T23:00:00+00:00"
    report = chunk_report(pending, identity=new_identity)
    runtime.receive(pending["version"], report.history, next_day, report=report)
    assert runtime.history_import.states["sensor.pv0"]["statistics_identity"] == new_identity
    assert (
        runtime.history_import.states["sensor.pv0"]["learning_start"]
        == configuration.sources[0].learning_start
    )
    assert runtime.histories["sensor.pv0"].hours[0].start == datetime(2026, 1, 4, 23, tzinfo=UTC)
    assert (
        runtime.document(next_day)["history_import"]["sensor.pv0"]["available_from_month"]
        == "2023-01-01T00:00:00+00:00"
    )
    assert documents.read(default_pointer) == default_reference
    assert documents.read("history-import-" + default_reference + ".json") == original_manifest
    assert documents.read(old_quarantine) == old_fault
    for descriptor in original_chunks:
        assert documents.exists("history-import-chunk-" + descriptor["reference"] + ".json")
    restored = SolarRuntime(SolarStore(documents))
    assert restored.histories == runtime.histories
    assert restored.history_issues == {}
    assert restored.history_import.states["sensor.pv0"]["statistics_identity"] == new_identity
    cleared = replace(
        configuration,
        sources=(replace(configuration.sources[0], learning_start=None), configuration.sources[1]),
    )
    revalidated = []
    chunk = restored.history_import._chunk

    def record(state, descriptor):
        """Revalidate the selected original cohort while the unchanged second source reuses its accepted aggregate."""
        revalidated.append((state["entity_id"], state.get("learning_start")))
        return chunk(state, descriptor)

    monkeypatch.setattr(restored.history_import, "_chunk", record)
    restored.store.save(cleared)
    restored.configure(cleared)
    assert revalidated == [("sensor.pv0", None)] * len(original_chunks)
    assert restored.histories == original_histories
    assert restored.history_issues["sensor.pv0"] == "history_continuity_unavailable"
    again = SolarRuntime(SolarStore(documents))
    assert again.histories == original_histories
    assert again.history_issues["sensor.pv0"] == "history_continuity_unavailable"


@pytest.mark.parametrize(
    ("boundary", "expected_start", "expected_year"),
    [
        ("2025-01-01T00:00:00+00:00", "2024-12-31T23:00:00+00:00", 2025),
        ("2025-12-31T23:45:00+00:00", "2025-12-31T23:00:00+00:00", 2026),
        ("2025-04-01T00:00:00+05:45", "2025-03-31T18:00:00+00:00", 2025),
    ],
)
def test_explicit_learning_cohort_requests_only_complete_later_hours_and_one_preceding_endpoint(
    tmp_path, boundary, expected_start, expected_year
):
    """Annual boundaries and quarter-offset timezones preserve exactly the endpoint needed for the first admissible hour."""
    runtime = runtime_case(tmp_path)
    configuration = replace(
        runtime.configuration,
        sources=(replace(runtime.configuration.sources[0], learning_start=boundary),),
    )
    runtime.store.save(configuration)
    runtime.configure(configuration)
    discover(runtime)
    pending = runtime.request(NOW)
    assert pending["start"] == expected_start
    assert pending["import_year"] == expected_year
    report = chunk_report(pending)
    runtime.receive(pending["version"], report.history, NOW, report=report)
    restarted = SolarRuntime(SolarStore(runtime.store.documents))
    assert restarted.histories == runtime.histories
    assert restarted.histories["sensor.pv0"].hours[0].start == datetime.fromisoformat(
        expected_start
    )


def test_changed_overlap_excludes_only_the_conflict_and_archives_both_transfers(tmp_path):
    """A conflicting year-boundary sum cannot reject the rest of either annual transfer."""
    runtime = runtime_case(tmp_path)
    discover(runtime)
    first = runtime.request(NOW)
    report = chunk_report(first)
    runtime.receive(first["version"], report.history, NOW, report=report)
    accepted = runtime.histories.copy()
    second = runtime.request(NOW)
    report = chunk_report(second, changed_overlap=True)
    runtime.receive(second["version"], report.history, NOW, report=report)
    conflict = datetime.fromisoformat(second["start"])
    hours = runtime.histories["sensor.pv0"].hours
    assert conflict not in {hour.start for hour in hours}
    assert set(accepted["sensor.pv0"].hours) - set(hours) == {
        hour for hour in accepted["sensor.pv0"].hours if hour.start == conflict
    }
    assert any(hour.start.year == 2024 for hour in hours)
    assert runtime.history_issues == {}
    assert len(list(runtime.store.documents.iter_prefix("history-import-chunk-"))) == 2
    restored = SolarRuntime(SolarStore(runtime.store.documents))
    assert restored.histories == runtime.histories
    assert restored.history_issues == {}


def test_changed_recorder_identity_preserves_the_cached_cohort(tmp_path):
    """A replaced statistics metadata ID cannot merge new readings into old meter history."""
    runtime = runtime_case(tmp_path)
    discover(runtime)
    finish(runtime)
    accepted = runtime.histories.copy()
    next_day = NOW + timedelta(days=1)
    pending = runtime.request(next_day)
    report = chunk_report(pending, identity={**IDENTITY, "metadata_id": 8})
    runtime.receive(pending["version"], report.history, next_day, report=report)
    assert runtime.histories == accepted
    assert runtime.history_issues["sensor.pv0"] == "history_continuity_unavailable"
    restored = SolarRuntime(SolarStore(runtime.store.documents))
    assert restored.histories == accepted
    assert restored.history_issues["sensor.pv0"] == "history_continuity_unavailable"
    pending = restored.request(next_day)
    restored.receive(pending["version"], None, next_day, error="history_unavailable")
    assert restored.history_issues["sensor.pv0"] == "history_continuity_unavailable"


def test_changed_previously_accepted_reading_excludes_only_its_endpoint(tmp_path):
    """A historical correction cannot silently change a target or block the newly captured day."""
    runtime = runtime_case(tmp_path)
    discover(runtime)
    finish(runtime)
    accepted = runtime.histories.copy()
    next_day = NOW + timedelta(days=1)
    pending = runtime.request(next_day)
    report = chunk_report(pending, changed_overlap=True)
    runtime.receive(pending["version"], report.history, next_day, report=report)
    conflict = datetime.fromisoformat(pending["start"])
    hours = runtime.histories["sensor.pv0"].hours
    assert conflict not in {hour.start for hour in hours}
    assert set(accepted["sensor.pv0"].hours) - set(hours) == {
        hour for hour in accepted["sensor.pv0"].hours if hour.start == conflict
    }
    assert len(hours) == len(accepted["sensor.pv0"].hours) + 23
    assert runtime.history_issues == {}


def test_aggregate_history_can_span_years_while_each_wire_transfer_remains_bounded():
    """An internal 9,001-row aggregate is valid while the same oversized remote report is rejected."""
    first = datetime(2023, 1, 1, tzinfo=UTC)
    rows = tuple(EnergyHour(first + timedelta(hours=index), index, index) for index in range(9001))
    assert len(EnergyHistory("sensor.pv0", rows).hours) == 9001
    with pytest.raises(ValueError, match="history size"):
        parse_history(
            {
                "request_version": "synthetic",
                "entity_id": "sensor.pv0",
                "unit": "kWh",
                "hours": [[row.start.timestamp(), row.total_kwh, row.meter_kwh] for row in rows],
            }
        )


def test_persistent_quarantine_revokes_old_worker_and_preserves_other_meter_history(tmp_path):
    """A changed source cannot train after restart, while its original rows and an unaffected meter remain available."""
    from tests.app.solar.support import SITE, weather

    runtime = runtime_case(tmp_path, count=2)
    discover(runtime)
    first = runtime.request(NOW)
    report = chunk_report(first)
    runtime.receive(first["version"], report.history, NOW, report=report)
    unaffected = EnergyHistory(
        "sensor.pv1",
        (
            EnergyHour(NOW.replace(hour=0), 10, 10),
            EnergyHour(NOW.replace(hour=1), 11, 11),
        ),
    )
    runtime.histories["sensor.pv1"] = unaffected
    runtime.set_weather(SITE, weather(NOW))
    queued = runtime.job(NOW)
    runtime.candidate = object()
    runtime.calibrations = (object(),)
    runtime.source_selection = {"pv0": {"active": "ml"}}
    pending = runtime.request(NOW)
    conflicting = chunk_report(pending, identity={**IDENTITY, "metadata_id": 99})
    runtime.receive(pending["version"], conflicting.history, NOW, report=conflicting)
    assert runtime.candidate is None
    assert runtime.calibrations == ()
    assert runtime.source_selection == {}
    assert not runtime.install((queued[0],), NOW)
    next_job = runtime.job(NOW)
    assert dict(next_job[4]) == {"sensor.pv1": unaffected}
    assert "sensor.pv0" in runtime.histories
    restored = SolarRuntime(SolarStore(runtime.store.documents))
    restored.set_weather(SITE, weather(NOW))
    assert "sensor.pv0" in restored.histories
    assert "sensor.pv0" not in dict(restored.job(NOW)[4])


def test_consistent_recheck_clears_quarantine_only_after_original_identity_and_rows_match(tmp_path):
    """Restoring the original Recorder cohort permits a bounded retry without rebinding replacement metadata."""
    runtime = runtime_case(tmp_path)
    discover(runtime)
    first = runtime.request(NOW)
    report = chunk_report(first)
    runtime.receive(first["version"], report.history, NOW, report=report)
    second = runtime.request(NOW)
    report = chunk_report(second, identity={**IDENTITY, "metadata_id": 99})
    runtime.receive(second["version"], report.history, NOW, report=report)
    restored = SolarRuntime(SolarStore(runtime.store.documents))
    pending = restored.request(NOW)
    assert pending == second
    correct = chunk_report(pending)
    restored.receive(pending["version"], correct.history, NOW, report=correct)
    assert "sensor.pv0" not in restored.history_issues
    assert "sensor.pv0" not in restored.history_import.issues
    again = SolarRuntime(SolarStore(restored.store.documents))
    assert "sensor.pv0" not in again.history_issues
    assert again.histories == restored.histories


def test_repeated_compatible_discovery_keeps_closed_chunks_and_changed_identity_is_quarantined(
    tmp_path,
):
    """A refreshed availability envelope retains accepted years and cannot silently install a replacement meter identity."""
    from open_energy_planner.solar.calculation.model import version
    from open_energy_planner.solar.storage.history import HistoryContinuityError

    runtime = runtime_case(tmp_path)
    initial = discover(runtime)
    finish(runtime)
    accepted = runtime.histories.copy()
    chunks = runtime.history_import.states["sensor.pv0"]["chunks"]
    same = parse_report(
        {
            "basis": "solar_history_range",
            "request_version": initial["version"],
            "entity_id": "sensor.pv0",
            "statistics_identity": IDENTITY,
            "available_start": "2023-01-01T00:00:00+00:00",
        }
    )
    restored = runtime.history_import.receive(initial, same, NOW)
    assert restored == accepted["sensor.pv0"]
    assert runtime.history_import.states["sensor.pv0"]["chunks"] == chunks
    assert runtime.request(NOW) is None
    changed = replace(same, statistics_identity={**IDENTITY, "metadata_id": 99})
    with pytest.raises(HistoryContinuityError):
        runtime.history_import.receive(initial, changed, NOW)
    assert runtime.history_import.states["sensor.pv0"]["chunks"] == chunks
    pointer = "history-import-quarantine-" + version("sensor.pv0") + ".json"
    assert runtime.store.documents.exists(pointer)
    again = SolarRuntime(SolarStore(runtime.store.documents))
    assert again.histories == accepted
    assert again.history_issues["sensor.pv0"] == "history_continuity_unavailable"
