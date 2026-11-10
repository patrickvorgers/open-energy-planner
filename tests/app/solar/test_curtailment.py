"""Apply actual curtailment feedback without discarding older unrestricted production."""

from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.solar.curtailment import (
    FEEDBACK_EPOCH,
    CurtailmentHistory,
    load_feedback,
    parse_feedback,
    save_feedback,
)
from open_energy_planner.solar.learning import learning_blocker, unrestricted_interval
from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.wire import configuration_document, parse_configuration
from tests.database.support.archive import make_store

from .support import START, plant


def test_switchable_source_without_feedback_uses_its_history():
    """Control permission alone does not exclude production from learning or scoring."""
    configuration = plant()
    source = replace(configuration.sources[0], can_curtail=True)
    assert learning_blocker(source, configuration.inverters[0]) is None
    assert unrestricted_interval(source, START, START + timedelta(hours=1))


@pytest.mark.parametrize(
    "minute,state,expected",
    [(0, True, False), (0, False, True), (30, True, False), (30, False, True), (60, True, True)],
)
def test_first_feedback_splits_the_hour_at_its_actual_timestamp(minute, state, expected):
    """A curtailed portion rejects the whole training hour, while an event at its end belongs to the next hour."""
    source = replace(
        plant().sources[0], can_curtail=True, curtailment_entity_id="binary_sensor.curtailed"
    )
    feedback = CurtailmentHistory(
        source.curtailment_entity_id,
        START + timedelta(hours=2),
        ((START + timedelta(minutes=minute), state),),
    )
    assert unrestricted_interval(source, START, START + timedelta(hours=1), feedback) is expected
    assert unrestricted_interval(
        source, START - timedelta(hours=2), START - timedelta(hours=1), feedback
    )


@pytest.mark.parametrize(
    "hour,expected", [(0, True), (1, False), (2, False), (3, True), (4, False)]
)
def test_observed_curtailment_unknown_and_recovery_do_not_fill_each_other(hour, expected):
    """On and unknown exclude hours after the first valid state, and only explicit off restores admission."""
    source = replace(plant().sources[0], curtailment_entity_id="binary_sensor.curtailed")
    feedback = CurtailmentHistory(
        source.curtailment_entity_id,
        START + timedelta(hours=4),
        tuple(
            (START + timedelta(hours=index), state)
            for index, state in enumerate((False, True, None, False))
        ),
    )
    assert (
        unrestricted_interval(
            source, START + timedelta(hours=hour), START + timedelta(hours=hour + 1), feedback
        )
        is expected
    )


def test_no_first_valid_feedback_uses_only_successfully_checked_history():
    """An empty successful read permits older production but neither unqueried future hours nor an unbound sensor."""
    source = replace(plant().sources[0], curtailment_entity_id="sensor.curtailment")
    assert not unrestricted_interval(source, START, START + timedelta(hours=1))
    feedback = CurtailmentHistory(
        source.curtailment_entity_id, START + timedelta(hours=1), ((START, None),)
    )
    assert unrestricted_interval(source, START, START + timedelta(hours=1), feedback)
    assert not unrestricted_interval(source, START, START + timedelta(hours=2), feedback)
    other = replace(feedback, entity_id="sensor.other")
    assert not unrestricted_interval(source, START, START + timedelta(hours=1), other)


@pytest.mark.parametrize(
    "damage", ["future", "numeric", "unordered", "duplicate", "private_state", "extra"]
)
def test_feedback_rejects_ambiguous_or_future_values(damage):
    """The wire boundary accepts explicit booleans and original ordered times without provider payloads."""
    body = {
        "entity_id": "binary_sensor.curtailed",
        "checked_until": (START + timedelta(hours=2)).isoformat(),
        "readings": [[START.isoformat(), False], [(START + timedelta(hours=1)).isoformat(), True]],
    }
    if damage == "future":
        body["checked_until"] = (START + timedelta(days=1)).isoformat()
    elif damage == "numeric":
        body["readings"][0][1] = 0
    elif damage == "unordered":
        body["readings"].reverse()
    elif damage == "duplicate":
        body["readings"][1][0] = START.isoformat()
    elif damage == "private_state":
        body["readings"][0][1] = "private-provider-value"
    else:
        body["provider"] = "private-provider-value"
    with pytest.raises(ValueError):
        parse_feedback(body, START + timedelta(hours=3))


def test_feedback_restart_preserves_the_first_observation_and_exact_sensor(tmp_path):
    """Incremental evidence survives reload without moving the first observation when earlier Recorder rows disappear."""
    store = make_store(tmp_path, SolarStore)
    first = CurtailmentHistory(
        "binary_sensor.curtailed", START + timedelta(hours=1), ((START, True),)
    )
    accepted = save_feedback(store, None, first, START - timedelta(hours=1))
    second = CurtailmentHistory(
        first.entity_id, START + timedelta(hours=3), ((START + timedelta(hours=2), False),)
    )
    accepted = save_feedback(store, accepted, second, first.checked_until)
    restored = load_feedback(
        make_store(tmp_path, SolarStore).documents, first.entity_id, START + timedelta(hours=4)
    )
    assert restored == accepted
    assert restored.first_valid == START
    assert not restored.unrestricted(START + timedelta(hours=1), START + timedelta(hours=2))
    assert restored.unrestricted(START + timedelta(hours=2), START + timedelta(hours=3))


def test_configuration_and_runtime_reject_a_delayed_reply_after_sensor_change(tmp_path):
    """Changing the linked sensor binds a new request and prevents old feedback from reaching another source."""
    configuration = plant()
    linked = replace(
        configuration,
        sources=(
            replace(configuration.sources[0], curtailment_entity_id="binary_sensor.curtailed"),
        ),
    )
    wire = configuration_document(linked)
    assert parse_configuration(wire) == linked
    assert linked.version != configuration.version
    store = make_store(tmp_path, SolarStore)
    store.save(linked)
    runtime = SolarRuntime(store)
    request = runtime.feedback_requests(START + timedelta(hours=1))[0]
    assert request["start"] == FEEDBACK_EPOCH.isoformat()
    body = {
        "configuration_version": linked.version,
        "start": request["start"],
        "feedback": {
            "entity_id": "binary_sensor.curtailed",
            "checked_until": request["end"],
            "readings": [],
        },
    }
    runtime.receive_feedback(body, START + timedelta(hours=2))
    assert runtime.feedback["binary_sensor.curtailed"].first_valid is None
    assert runtime.feedback["binary_sensor.curtailed"].checked_until.isoformat() == request["end"]
    runtime.configure(configuration)
    with pytest.raises(ValueError, match="Invalid or outdated solar curtailment feedback"):
        runtime.receive_feedback(body, START + timedelta(hours=2))
    assert runtime.feedback == {}


def test_multiple_empty_sensor_histories_keep_independent_source_boundaries(tmp_path):
    """Equal empty observation sets cannot collide or let one sensor extend another's checked range."""
    configuration = plant(count=2)
    configuration = replace(
        configuration,
        sources=tuple(
            replace(source, curtailment_entity_id=f"binary_sensor.curtailed{index}")
            for index, source in enumerate(configuration.sources)
        ),
    )
    store = make_store(tmp_path, SolarStore)
    store.save(configuration)
    runtime = SolarRuntime(store)
    requests = runtime.feedback_requests(START)
    assert len(requests) == 2
    for request in requests:
        runtime.receive_feedback(
            {
                "configuration_version": configuration.version,
                "start": request["start"],
                "feedback": {
                    "entity_id": request["entity_id"],
                    "checked_until": request["end"],
                    "readings": [],
                },
            },
            START,
        )
    restored = SolarRuntime(make_store(tmp_path, SolarStore))
    assert restored.feedback == runtime.feedback
    assert set(restored.feedback) == {"binary_sensor.curtailed0", "binary_sensor.curtailed1"}


def test_later_feedback_cannot_reclassify_an_earlier_curtailed_hour():
    """A later off observation never grants unrestricted production to a preceding on interval."""
    history = CurtailmentHistory(
        "binary_sensor.curtailed",
        START + timedelta(hours=3),
        ((START, True), (START + timedelta(hours=2), False)),
    )
    assert history.unrestricted(START - timedelta(hours=1), START)
    assert not history.unrestricted(START, START + timedelta(hours=1))
    assert not history.unrestricted(START + timedelta(hours=1), START + timedelta(hours=2))
    assert history.unrestricted(START + timedelta(hours=2), START + timedelta(hours=3))


def test_unchanged_feedback_refresh_does_not_trigger_another_model_fit(tmp_path):
    """Refreshing checked coverage inside the same completed hour preserves the active forecast and training revision."""
    configuration = plant()
    source = replace(configuration.sources[0], curtailment_entity_id="binary_sensor.curtailed")
    configuration = replace(configuration, sources=(source,))
    store = make_store(tmp_path, SolarStore)
    store.save(configuration)
    first = CurtailmentHistory(
        source.curtailment_entity_id, START + timedelta(minutes=5), ((START, False),)
    )
    save_feedback(store, None, first, START - timedelta(hours=1))
    runtime = SolarRuntime(store)
    original = runtime.feedback_revision
    active = object()
    runtime.forecast = active
    runtime.receive_feedback(
        {
            "configuration_version": configuration.version,
            "start": first.checked_until.isoformat(),
            "feedback": {
                "entity_id": source.curtailment_entity_id,
                "checked_until": (START + timedelta(minutes=10)).isoformat(),
                "readings": [],
            },
        },
        START + timedelta(minutes=11),
    )
    assert runtime.feedback_revision == original
    assert runtime.forecast is active
