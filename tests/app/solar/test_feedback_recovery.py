"""Bound repeated feedback work and recover retained evidence without configuration changes."""

import asyncio
import json
import logging
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.solar import curtailment
from open_energy_planner.solar.curtailment import CurtailmentHistory, load_feedback, save_feedback
from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.wire import configuration_document
from tests.app.forecast.support.http import configure_forecasts, running_forecasts
from tests.app.solar.support import START, fixture_weather, plant
from tests.database.support.archive import list_documents, make_store


@pytest.mark.parametrize("append_count", [80, 160])
def test_repeated_fitting_retains_only_linear_feedback_lookup_arrays(tmp_path, append_count):
    """Materializing every live prefix must not retain every old lookup array through its parent chain."""
    store = make_store(tmp_path, SolarStore)
    entity, history = "binary_sensor.materialized", None
    retained = retained_readings = retained_times = None
    expected = []
    for index in range(append_count):
        at = START + timedelta(minutes=index)
        state = (False, True, None)[index % 3]
        expected.append((at, state))
        history = save_feedback(
            store,
            history,
            CurtailmentHistory(entity, at + timedelta(minutes=1), ((at, state),)),
            history.checked_until if history else START - timedelta(minutes=1),
        )
        assert history.unrestricted(at, at + timedelta(seconds=30)) is (state is False)
        if index == 19:
            retained, retained_readings, retained_times = history, history.readings, history.times
    arrays, node = {}, history
    while node is not None:
        for name in ("readings", "times"):
            if name in node.__dict__:
                value = node.__dict__[name]
                arrays[id(value)] = value
        node = node.parent
    assert sum(len(value) for value in arrays.values()) <= 2 * append_count
    assert history.readings == tuple(expected)
    assert retained.readings is retained_readings
    assert retained.times is retained_times
    assert retained.readings == tuple(expected[:20])


@pytest.mark.parametrize("empty_archive", [False, True])
def test_failed_restore_without_observations_stays_pending_until_capture(
    tmp_path, monkeypatch, caplog, empty_archive
):
    """A readable absent or empty archive is not recovered evidence and must not prevent real capture."""
    from open_energy_planner.solar import runtime

    caplog.set_level(logging.DEBUG, logger="open_energy_planner.solar.runtime")
    entity = "binary_sensor.pending"
    configuration = plant()
    configuration = replace(
        configuration, sources=(replace(configuration.sources[0], curtailment_entity_id=entity),)
    )
    store = make_store(tmp_path, SolarStore)
    store.save(configuration)
    if empty_archive:
        save_feedback(
            store, None, CurtailmentHistory(entity, START, ()), START - timedelta(hours=1)
        )
    original = runtime.load_feedback
    failed = True

    def transient_read(documents, source, now):
        """Keep the original store reader after one controlled private storage failure."""
        if failed:
            raise OSError("RESTORE_EMPTY_PRIVATE_SENTINEL")
        return original(documents, source, now)

    monkeypatch.setattr(runtime, "load_feedback", transient_read)
    restored_runtime = SolarRuntime(store)
    assert entity in restored_runtime.feedback_issues
    now = restored_runtime.feedback_retry_after[entity]
    failed = False
    job = restored_runtime.feedback_restore_job(now)
    result = restored_runtime.read_feedback(job[1], now)
    restored_runtime.install_feedback_restore(job, result)
    assert not restored_runtime.feedback_issues
    request = restored_runtime.feedback_requests(now)[0]
    assert request["entity_id"] == entity
    assert "recovered" not in caplog.text.lower()
    assert "ready." not in caplog.text.lower()
    for _ in range(3):
        restored_runtime.read_feedback((entity,), now)
    pending = [row for row in caplog.records if "no retained observations" in row.getMessage()]
    assert sum(row.levelno == logging.INFO for row in pending) == 1
    assert sum(row.levelno == logging.DEBUG for row in pending) == 3
    restored_runtime.receive_feedback(
        {
            "configuration_version": configuration.version,
            "start": request["start"],
            "feedback": {
                "entity_id": entity,
                "checked_until": request["end"],
                "readings": [[request["start"], None]],
            },
        },
        now,
    )
    for _ in range(2):
        restored_runtime.read_feedback((entity,), now)
    recovered = [row for row in caplog.records if "restore recovered." in row.getMessage()]
    assert len(recovered) == 1
    assert recovered[0].levelno == logging.INFO
    assert "RESTORE_EMPTY_PRIVATE_SENTINEL" not in caplog.text


def test_empty_and_dense_feedback_updates_have_linear_archive_work(tmp_path, monkeypatch):
    """Empty updates retain no new immutable copies. Dense appends hash and store only their new observations."""
    store = make_store(tmp_path, SolarStore)
    entity = "binary_sensor.curtailed"
    history = save_feedback(
        store,
        None,
        CurtailmentHistory(
            entity,
            START,
            tuple((START - timedelta(seconds=10000 - index), False) for index in range(10000)),
        ),
        START - timedelta(days=1),
    )
    before = {
        name
        for name in list_documents(tmp_path, "solar")
        if not name.startswith("curtailment-current-")
    }
    hashed_bytes, read_count = 0, 0
    original_version, original_read = curtailment.version, store.documents.read

    def counted_version(value):
        """Count actual serialized hash input independently of elapsed time."""
        nonlocal hashed_bytes
        hashed_bytes += len(json.dumps(value))
        return original_version(value)

    def counted_read(name):
        """Count actual archive reads rather than trusting an implementation counter."""
        nonlocal read_count
        read_count += 1
        return original_read(name)

    monkeypatch.setattr(curtailment, "version", counted_version)
    monkeypatch.setattr(store.documents, "read", counted_read)
    for index in range(1, 31):
        until = START + timedelta(minutes=5 * index)
        history = save_feedback(
            store, history, CurtailmentHistory(entity, until, ()), history.checked_until
        )
    assert {
        name
        for name in list_documents(tmp_path, "solar")
        if not name.startswith("curtailment-current-")
    } == before
    assert hashed_bytes < 30000
    assert read_count <= 60
    hashed_bytes, read_count = 0, 0
    for index in range(30):
        at = history.checked_until
        history = save_feedback(
            store,
            history,
            CurtailmentHistory(entity, at + timedelta(minutes=5), ((at, bool(index % 2)),)),
            at,
        )
    assert hashed_bytes < 50000
    assert read_count <= 90
    after = {
        name
        for name in list_documents(tmp_path, "solar")
        if not name.startswith("curtailment-current-")
    }
    assert len(after - before) <= 30
    restored = load_feedback(store.documents, entity, history.checked_until + timedelta(hours=1))
    assert restored == history
    assert len(restored.readings) == 10030
    assert restored.first_valid == START - timedelta(seconds=10000)


def test_empty_recorder_history_catches_up_in_finite_checked_ranges(tmp_path):
    """An entirely empty Recorder advances from the epoch to now without immutable polling receipts."""
    configuration = plant()
    entity = "binary_sensor.empty"
    configuration = replace(
        configuration, sources=(replace(configuration.sources[0], curtailment_entity_id=entity),)
    )
    store = make_store(tmp_path, SolarStore)
    store.save(configuration)
    runtime = SolarRuntime(store)
    previous, transfers = None, 0
    for _ in range(80):
        requests = runtime.feedback_requests(START)
        if not requests:
            break
        request = requests[0]
        assert request["start"] != previous
        runtime.receive_feedback(
            {
                "configuration_version": configuration.version,
                "start": request["start"],
                "feedback": {"entity_id": entity, "checked_until": request["end"], "readings": []},
            },
            START,
        )
        previous = request["start"]
        transfers += 1
    assert 50 < transfers < 60
    assert runtime.feedback[entity].checked_until == START
    assert runtime.feedback[entity].first_valid is None
    assert runtime.feedback[entity].unrestricted(
        START - timedelta(hours=2), START - timedelta(hours=1)
    )
    assert not list(store.documents.iter_prefix("curtailment-history-"))
    assert not list(store.documents.iter_prefix("curtailment-segment-"))


@pytest.mark.parametrize("failure_kind", [OSError, ValueError, KeyError, TypeError])
def test_transient_feedback_restore_recovers_in_background_without_save(
    tmp_path, monkeypatch, caplog, failure_kind
):
    """A private transient storage failure is retried and recovered without blocking a healthy sensor."""
    from open_energy_planner.solar import api, runtime

    caplog.set_level(logging.DEBUG, logger="open_energy_planner.solar.runtime")

    available = False
    attempts = []
    original = runtime.load_feedback

    def temporarily_unreadable(documents, entity, now):
        """Fail only one source with a private value until the fixture restores its storage."""
        if entity == "binary_sensor.broken":
            attempts.append(now)
            if not available:
                raise failure_kind("https://private.invalid/?token=RESTORE_RETRY_PRIVATE_SENTINEL")
        return original(documents, entity, now)

    monkeypatch.setattr(runtime, "load_feedback", temporarily_unreadable)
    monkeypatch.setattr(runtime, "FEEDBACK_RESTORE_RETRY_SECONDS", 0.2, raising=False)
    monkeypatch.setattr(api, "SOLAR_WORKER_POLL_SECONDS", 0.01)

    def no_model_job(_runtime, _now):
        """Keep unrelated physical forecast calculation from delaying this background recovery fixture."""
        return

    monkeypatch.setattr(runtime.SolarRuntime, "job", no_model_job)

    async def scenario():
        """Exercise the actual update lifecycle and authenticated producer projection."""
        nonlocal available
        save_feedback(
            make_store(tmp_path, SolarStore),
            None,
            CurtailmentHistory(
                "binary_sensor.broken", START + timedelta(hours=1), ((START, False),)
            ),
            START - timedelta(hours=1),
        )
        configuration = plant(count=2)
        configuration = replace(
            configuration,
            sources=tuple(
                replace(source, curtailment_entity_id=f"binary_sensor.{name}")
                for source, name in zip(configuration.sources, ("broken", "healthy"), strict=True)
            ),
        )
        async with running_forecasts(tmp_path, weather_factory=fixture_weather) as (session, url):
            definition = configuration_document(configuration)
            definition.pop("version")
            configured = await configure_forecasts(session, url, solar_definition=definition)
            assert "binary_sensor.broken" in configured["solar"]["feedback_issues"]
            healthy = next(
                row
                for row in configured["solar"]["feedback_requests"]
                if row["entity_id"] == "binary_sensor.healthy"
            )
            async with session.post(
                url + "/v1/solar/feedback",
                json={
                    "configuration_version": configuration.version,
                    "start": healthy["start"],
                    "feedback": {
                        "entity_id": healthy["entity_id"],
                        "checked_until": healthy["end"],
                        "readings": [],
                    },
                },
            ) as response:
                assert response.status == 200
            initial_attempts = len(attempts)
            await asyncio.sleep(0.03)
            for _ in range(3):
                async with session.get(url + "/v1/solar") as response:
                    assert response.status == 200
            assert len(attempts) == initial_attempts
            for _ in range(30):
                if len(attempts) > initial_attempts:
                    break
                await asyncio.sleep(0.02)
            assert len(attempts) > initial_attempts
            available = True
            recovered = None
            for _ in range(30):
                await asyncio.sleep(0.02)
                async with session.get(url + "/v1/solar") as response:
                    recovered = await response.json()
                assert "RESTORE_RETRY_PRIVATE_SENTINEL" not in json.dumps(recovered)
                if not recovered["feedback_issues"]:
                    break
            assert recovered["feedback_issues"] == {}
            assert any(
                row["entity_id"] == "binary_sensor.broken" for row in recovered["feedback_requests"]
            )
            assert len(attempts) >= 3

    asyncio.run(scenario())
    assert "RESTORE_RETRY_PRIVATE_SENTINEL" not in caplog.text
    assert "recovered" in caplog.text.lower()
    failures = [
        row
        for row in caplog.records
        if row.getMessage().startswith("Solar curtailment feedback restore failed")
    ]
    assert sum(row.levelno == logging.WARNING for row in failures) == 1
    assert any(row.levelno == logging.DEBUG for row in failures)
    assert "restore traceback" in caplog.text


def test_long_feedback_restart_batches_integrity_and_referenced_receipt(tmp_path, monkeypatch):
    """1,100 appends restore in nine bounded batches, support repeated lookups and retain an older referenced prefix."""
    from open_energy_planner.solar.curtailment import archive_feedback

    store = make_store(tmp_path, SolarStore)
    entity, history, retained_reference = "binary_sensor.retained", None, None
    for index in range(1100):
        at = START + timedelta(minutes=index)
        history = save_feedback(
            store,
            history,
            CurtailmentHistory(
                entity, at + timedelta(minutes=1), ((at, (False, True, None)[index % 3]),)
            ),
            history.checked_until if history else START - timedelta(minutes=1),
        )
        if index == 99:
            retained_reference = archive_feedback(store, history)
    restored_store = make_store(tmp_path, SolarStore)
    batches = []
    original_read_many = restored_store.documents.read_many

    def bounded_read(names):
        """Observe real archive query batches rather than copying the implementation's bound."""
        names = tuple(names)
        assert len(names) <= 128
        batches.append(len(names))
        return original_read_many(names)

    monkeypatch.setattr(restored_store.documents, "read_many", bounded_read)
    now = START + timedelta(days=2)
    restored = load_feedback(restored_store.documents, entity, now)
    assert batches == [128] * 8 + [76]
    assert restored.first_valid == START
    assert restored.checked_until == START + timedelta(minutes=1100)
    assert len(restored.readings) == 1100
    snapshot = restored.readings
    for index in range(1000):
        at = START + timedelta(minutes=index)
        assert restored.unrestricted(at, at + timedelta(seconds=20)) is (index % 3 == 0)
        assert restored.readings is snapshot
        assert restored.reference == history.reference
    assert batches == [128] * 8 + [76]
    retained = load_feedback(restored_store.documents, entity, now, reference=retained_reference)
    assert retained.checked_until == START + timedelta(minutes=100)
    assert len(retained.readings) == 100
    assert retained.readings == restored.readings[:100]

    segment_name = "curtailment-segment-" + curtailment.version(entity) + "-500.json"
    original = restored_store.documents.read(segment_name)
    corrupted = deepcopy(original)
    corrupted["readings"][0][1] = False
    restored_store.documents.write(segment_name, corrupted)
    with pytest.raises(ValueError):
        load_feedback(restored_store.documents, entity, now)
    assert (
        load_feedback(restored_store.documents, entity, now, reference=retained_reference)
        == retained
    )
    restored_store.documents.write(segment_name, original)
    before = list_documents(tmp_path, "solar")
    original_write = restored_store.documents.write

    def interrupt_pointer(name, value):
        """Fail only the final mutable publication after preparing the next immutable segment."""
        if name.startswith("curtailment-current-"):
            raise OSError("synthetic pointer interruption")
        original_write(name, value)

    monkeypatch.setattr(restored_store.documents, "write", interrupt_pointer)
    with pytest.raises(OSError):
        save_feedback(
            restored_store,
            restored,
            CurtailmentHistory(entity, now, ((restored.checked_until, True),)),
            restored.checked_until,
        )
    assert list_documents(tmp_path, "solar") == before
    assert load_feedback(restored_store.documents, entity, now) == restored
