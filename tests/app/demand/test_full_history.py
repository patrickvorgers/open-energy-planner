"""Use all available appliance years while excluding untrustworthy intervals individually."""

from datetime import UTC, datetime, timedelta

from open_energy_planner.demand.calculation.model import DemandConfiguration, EnergyTerm
from open_energy_planner.demand.reconstruction import DemandReconstructor
from open_energy_planner.demand.runtime import DemandRuntime
from open_energy_planner.demand.storage import DemandStore
from open_energy_planner.history.model import EnergyHistory, EnergyHour
from tests.database.support.archive import make_store


def test_annual_import_validates_each_immutable_chunk_once(tmp_path, monkeypatch, request):
    """Three annual receipts require three chunk reads and six normalizations, including their wire reports."""
    import json

    from sqlalchemy import event

    from open_energy_planner.history import importer
    from open_energy_planner.history.importer import CumulativeHistory, EnergyEndpoint
    from tests.app.demand.history_support import receive_source

    store = make_store(tmp_path, DemandStore)
    store.save(DemandConfiguration("Europe/Amsterdam", 4000, (EnergyTerm("sensor.house", 1),)))
    runtime = DemandRuntime(store)
    history = CumulativeHistory(
        "sensor.house",
        tuple(
            EnergyEndpoint(datetime(year, 1, 1, hour, tzinfo=UTC), (year - 2020) * 100 + hour)
            for year in (2024, 2025, 2026)
            for hour in (0, 1)
        ),
    )
    counts = {"chunk_reads": 0, "chunk_queries": 0, "normalizations": 0}
    original_read, original_normalize = store.documents.read, importer._normalized_hours

    def read(name):
        """Count actual source-chunk document reads without changing returned evidence."""
        if name.startswith("history-import-chunk-"):
            counts["chunk_reads"] += 1
        return original_read(name)

    def normalize(rows):
        """Count each full normalization, including incoming wire data."""
        counts["normalizations"] += 1
        return original_normalize(rows)

    def query(_connection, _cursor, statement, parameters, _context, _many):
        """Count real SELECTs bound to a chunk, including its immutable existence check."""
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if statement.lstrip().upper().startswith("SELECT") and any(
            isinstance(value, str) and value.startswith("history-import-chunk-") for value in values
        ):
            counts["chunk_queries"] += 1

    monkeypatch.setattr(store.documents, "read", read)
    monkeypatch.setattr(importer, "_normalized_hours", normalize)
    event.listen(store.documents.engine, "before_cursor_execute", query)
    try:
        receive_source(runtime, history, datetime(2026, 6, 1, tzinfo=UTC))
    finally:
        event.remove(store.documents.engine, "before_cursor_execute", query)
    request.node.user_properties.append(("observed_import_counts", json.dumps(counts)))
    assert runtime.histories == {"sensor.house": history}
    assert counts == {"chunk_reads": 3, "chunk_queries": 9, "normalizations": 6}


def test_consumer_import_resumes_without_activating_partial_years(tmp_path):
    """One meter completes all years before activation and restores its accepted cohort on restart."""
    from open_energy_planner.appliances.forecasting import ConsumerForecasts
    from open_energy_planner.history.importer import CumulativeHistory, EnergyEndpoint, parse_report
    from tests.app.demand.history_support import history_report

    store = make_store(tmp_path, DemandStore)
    configuration = DemandConfiguration("Europe/Amsterdam", 4000, (EnergyTerm("sensor.house", 1),))
    consumers = ConsumerForecasts(store.documents)
    catalogue = {
        "entities": [],
        "suggestions": {"consumers": [{"entity_id": "sensor.heat", "upstream": None}]},
    }
    consumers.sync(catalogue)
    consumers.save(({"entity_id": "sensor.heat", "weather_sensitive": True},), consumers.version)
    history = CumulativeHistory(
        "sensor.heat",
        tuple(
            EnergyEndpoint(datetime(year, 6, 1, tzinfo=UTC), (year - 2020) * 100)
            for year in range(2021, 2025)
        ),
    )
    for _ in range(2):
        pending = consumers.request(configuration, NOW)
        consumers.receive(pending, parse_report(history_report(pending, history)), now=NOW)
    assert consumers.histories == {}
    consumers = ConsumerForecasts(store.documents)
    consumers.sync(catalogue)
    assert consumers.histories == {}
    imported_years = []
    while pending := consumers.request(configuration, NOW):
        imported_years.append(pending["import_year"])
        consumers.receive(pending, parse_report(history_report(pending, history)), now=NOW)
    assert imported_years == [2022, 2023, 2024]
    assert consumers.histories["sensor.heat"] == history
    restored = ConsumerForecasts(store.documents)
    restored.sync(catalogue)
    assert restored.histories == consumers.histories
    later = NOW + timedelta(days=1)
    pending = restored.request(configuration, later)
    restored.receive(pending, error="history_unavailable", now=later)
    assert restored.histories == consumers.histories
    assert restored.request(configuration, later) is None
    assert restored.request(configuration, later + timedelta(minutes=15)) == pending


def test_household_restart_preserves_the_complete_cohort_during_one_meter_refresh(tmp_path):
    """Restart never mixes a freshly received meter with the previous household source cohort."""
    from open_energy_planner.history.importer import CumulativeHistory, EnergyEndpoint
    from tests.app.demand.history_support import receive_source

    store = make_store(tmp_path, DemandStore)
    store.save(
        DemandConfiguration(
            "Europe/Amsterdam",
            4000,
            (
                EnergyTerm("sensor.import", 1),
                EnergyTerm("sensor.export", -1),
            ),
        )
    )
    runtime = DemandRuntime(store)
    anchor = datetime(2024, 6, 1, tzinfo=UTC)
    histories = {
        entity: CumulativeHistory(
            entity,
            tuple(
                EnergyEndpoint(anchor + timedelta(hours=index), index * rate) for index in range(4)
            ),
        )
        for entity, rate in (("sensor.import", 2), ("sensor.export", 1))
    }
    while pending := runtime.request(NOW):
        receive_source(runtime, histories[pending["entity_id"]], NOW)
    accepted = runtime.histories.copy()
    assert len(accepted) == 2
    later = NOW + timedelta(days=1)
    entity = runtime.request(later)["entity_id"]
    from dataclasses import replace

    revised = replace(
        histories[entity],
        hours=histories[entity].hours + (EnergyEndpoint(anchor + timedelta(hours=4), 8),),
    )
    receive_source(runtime, revised, later)
    assert runtime.histories == accepted
    restored = DemandRuntime(DemandStore(store.documents))
    assert restored.histories == accepted
    assert restored.pending_histories[entity] == revised


def test_failed_meter_does_not_starve_an_independent_household_source(tmp_path):
    """A retry delay changes scheduling without changing accepted training evidence."""
    store = make_store(tmp_path, DemandStore)
    store.save(
        DemandConfiguration(
            "Europe/Amsterdam",
            4000,
            (
                EnergyTerm("sensor.import", 1),
                EnergyTerm("sensor.export", -1),
            ),
        )
    )
    runtime = DemandRuntime(store)
    pending = runtime.request(NOW)
    runtime.history_failed(pending, "history_unavailable", NOW)
    assert runtime.request(NOW)["entity_id"] != pending["entity_id"]
    assert runtime.request(NOW + timedelta(minutes=15)) == pending
    assert runtime.histories == {}


START = datetime(2021, 1, 1, tzinfo=UTC)
NOW = datetime(2024, 11, 8, 12, tzinfo=UTC)


def test_full_history_import_resumes_all_years_without_expanding_one_transfer(tmp_path):
    """Four bounded years survive restart, retain a gap and refresh only the open year."""
    store = make_store(tmp_path, DemandStore)
    store.save(DemandConfiguration("Europe/Amsterdam", 4000, (EnergyTerm("sensor.house", 1),)))
    runtime = DemandRuntime(store)
    pending = runtime.request(NOW)
    assert pending.get("basis") == "energy_history_range"
    assert pending["start"] == "1970-01-01T00:00:00+00:00"
    from open_energy_planner.history.importer import parse_report

    identity = {"metadata_id": 17, "source": "recorder", "unit": "kWh"}
    report = parse_report(
        {
            "request_version": pending["version"],
            "basis": pending["basis"],
            "entity_id": "sensor.house",
            "statistics_identity": identity,
            "available_start": START.isoformat(),
        }
    )
    runtime.receive(pending["version"], report, NOW)
    years = []
    missing = datetime(2022, 7, 2, 12, tzinfo=UTC)
    while pending := runtime.request(NOW):
        assert pending["basis"] == "energy_history_chunk"
        years.append(pending["import_year"])
        start, end = (datetime.fromisoformat(pending[key]) for key in ("start", "end"))
        assert end - start <= timedelta(days=367)
        hours = []
        instant = start
        while instant < end:
            if instant >= START and instant != missing:
                cumulative = (instant - START).total_seconds() / 3600
                hours.append([instant.timestamp(), cumulative, cumulative])
            instant += timedelta(hours=1)
        assert len(hours) <= 9000
        report = parse_report(
            {
                "request_version": pending["version"],
                "basis": pending["basis"],
                "entity_id": "sensor.house",
                "statistics_identity": identity,
                "unit": "kWh",
                "hours": hours,
            }
        )
        runtime.receive(pending["version"], report, NOW)
    assert years == [2021, 2022, 2023, 2024]
    history = runtime.histories["sensor.house"]
    assert len(history.hours) > 26000
    assert missing not in {hour.start for hour in history.hours}
    restored = DemandRuntime(DemandStore(store.documents))
    assert restored.histories == runtime.histories
    assert restored.request(NOW) is None
    assert restored.request(NOW + timedelta(days=1))["import_year"] == 2024


def test_scattered_invalid_hours_do_not_discard_years_of_valid_training():
    """Thirty impossible export hours are excluded while the remaining three years stay usable."""
    end = datetime(2024, 1, 1, tzinfo=UTC)
    hours = int((end - START).total_seconds() / 3600)
    broken = {240 + 600 * index for index in range(30)}
    imported, exported = [], []
    cumulative_export = 0
    for index in range(hours + 1):
        instant = START + timedelta(hours=index)
        imported.append(EnergyHour(instant, index, index))
        if index - 1 in broken:
            cumulative_export += 2
        exported.append(EnergyHour(instant, cumulative_export, cumulative_export))
    configuration = DemandConfiguration(
        "Europe/Amsterdam",
        4000,
        (EnergyTerm("sensor.import", 1), EnergyTerm("sensor.export", -1)),
    )
    histories = (
        EnergyHistory("sensor.import", tuple(imported)),
        EnergyHistory("sensor.export", tuple(exported)),
    )
    samples = DemandReconstructor().reconstruct(
        configuration, histories, before=end + timedelta(hours=1)
    )
    assert len(samples) == hours - 30
    assert all(sample.total_kwh == sample.residual_kwh == 1 for sample in samples)
    assert not {START + timedelta(hours=index + 1) for index in broken} & {
        sample.start for sample in samples
    }


def test_gap_jump_is_excluded_and_the_next_contiguous_interval_is_usable():
    """The accumulated 20 kWh after a gap is omitted. The next measured 2 kWh remains valid."""
    history = EnergyHistory(
        "sensor.house",
        tuple(
            EnergyHour(START + timedelta(hours=hour), total, total)
            for hour, total in ((0, 0), (1, 1), (5, 21), (6, 23))
        ),
    )
    configuration = DemandConfiguration("Europe/Amsterdam", 4000, (EnergyTerm("sensor.house", 1),))
    samples = DemandReconstructor().reconstruct(
        configuration, (history,), before=START + timedelta(days=1)
    )
    assert {sample.start: sample.residual_kwh for sample in samples} == {
        START + timedelta(hours=1): 1,
        START + timedelta(hours=6): 2,
    }
