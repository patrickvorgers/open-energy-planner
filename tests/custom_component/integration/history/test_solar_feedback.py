"""Transport real Recorder feedback without converting unknown states to unrestricted production."""

import asyncio
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.util import dt as dt_util

from tests.custom_component.support.chain import running_chain


@pytest.mark.parametrize("retained_state", ["on", "off", "unavailable"])
def test_real_recorder_first_retained_attribute_observation_keeps_actual_time(
    monkeypatch, retained_state
):
    """Purging the original transition must not hide the earliest retained attribute-only observation."""
    from homeassistant.components.recorder.db_schema import States
    from homeassistant.components.recorder.util import session_scope
    from sqlalchemy import delete, select, update

    from custom_components.open_energy_planner import solar_feedback
    from custom_components.open_energy_planner.solar_feedback import capture
    from open_energy_planner.solar.curtailment import parse_feedback

    async def scenario():
        """Persist native HA events, then remove only the original synthetic transition as a purge would."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            recorder = get_instance(hass)
            entity = "binary_sensor.synthetic_curtailment"
            observations = []
            for state, attributes in (
                (retained_state, {"sample": 1}),
                (retained_state, {"sample": 2}),
                ("on" if retained_state == "off" else "off", {"sample": 3}),
            ):
                hass.states.async_set(entity, state, attributes)
                observations.append(hass.states.get(entity))
                await hass.async_block_till_done()
                await recorder.async_block_till_done()
            original, retained, transition = observations
            assert retained.last_changed == original.last_changed
            assert retained.last_updated > original.last_updated

            def purge_original():
                """Delete one owned test row without changing the retained event's native timestamp."""
                with session_scope(hass=hass) as session:
                    metadata_id = recorder.states_meta_manager.get(entity, session, False)
                    stored = session.execute(
                        select(States.state_id, States.last_updated_ts)
                        .where(States.metadata_id == metadata_id)
                        .order_by(States.last_updated_ts)
                    ).all()
                    assert len(stored) == 3
                    session.execute(
                        update(States)
                        .where(States.metadata_id == metadata_id)
                        .values(old_state_id=None)
                    )
                    deleted = session.execute(
                        delete(States).where(
                            States.metadata_id == metadata_id,
                            States.state_id == stored[0].state_id,
                        )
                    )
                    assert deleted.rowcount == 1
                    remaining = (
                        session.execute(
                            select(States.last_updated_ts).where(States.metadata_id == metadata_id)
                        )
                        .scalars()
                        .all()
                    )
                    assert remaining == [stored[1].last_updated_ts, stored[2].last_updated_ts]
                    return [dt_util.utc_from_timestamp(row.last_updated_ts) for row in stored]

            stored_times = await recorder.async_add_executor_job(purge_original)
            request = {
                "entity_id": entity,
                "configuration_version": "c" * 64,
                "start": (original.last_updated - timedelta(seconds=1)).isoformat(),
                "end": dt_util.utcnow().isoformat(),
            }
            response = await capture(hass, request)
            expected = [
                [stored_times[1].isoformat(), {"on": True, "off": False}.get(retained_state)],
                [stored_times[2].isoformat(), transition.state == "on"],
            ]
            assert response["feedback"]["readings"] == expected
            history = parse_feedback(response["feedback"], dt_util.utcnow())
            assert history.first_valid == stored_times[2 if retained_state == "unavailable" else 1]
            assert history.unrestricted(stored_times[1], stored_times[2]) is (
                retained_state != "on"
            )
            resumed = await capture(hass, {**request, "start": stored_times[1].isoformat()})
            assert resumed["feedback"]["readings"] == expected
            monkeypatch.setattr(solar_feedback, "MAX_FEEDBACK_READINGS", 1)
            bounded = await capture(hass, request)
            assert bounded["feedback"]["readings"] == expected[:1]
            assert bounded["feedback"]["checked_until"] == stored_times[2].isoformat()
            continuation = await capture(
                hass, {**request, "start": bounded["feedback"]["checked_until"]}
            )
            assert continuation["feedback"]["readings"] == expected[1:]

    asyncio.run(scenario())


def test_oversized_feedback_advances_in_exact_exclusive_chunks(monkeypatch):
    """More than 10,000 observations progress without skipping the transition at the next cursor."""
    from custom_components.open_energy_planner import solar_feedback

    async def scenario():
        """Emulate Recorder's strict lower SQL boundary and retain original observation timestamps."""
        start = dt_util.utcnow() - timedelta(days=2)
        end = start + timedelta(days=1)
        entity = "binary_sensor.synthetic_curtailment"
        observations = [
            SimpleNamespace(
                last_updated=start + timedelta(seconds=index),
                state=("off", "on", "unavailable")[index % 3],
            )
            for index in range(10005)
        ]
        query_sizes = []

        async def committed():
            """This isolated query fixture has no queued transactions."""

        async def execute(query):
            """Execute the supplied Recorder query without another thread in this transport fixture."""
            return query()

        def history(_hass, lower, upper, entities, **options):
            """Match pinned Recorder's strict SQL boundaries, including the next chunk's exact-start trap."""
            assert entities in ([entity], entity)
            rows = [row for row in observations if lower < row.last_updated < upper]
            rows = rows[: options.get("limit", len(rows))]
            query_sizes.append(len(rows))
            return {entity: rows}

        recorder = SimpleNamespace(async_block_till_done=committed, async_add_executor_job=execute)
        monkeypatch.setattr(solar_feedback, "get_instance", lambda _hass: recorder)
        monkeypatch.setattr(solar_feedback, "get_significant_states", history, raising=False)
        monkeypatch.setattr(solar_feedback, "state_changes_during_period", history, raising=False)
        monkeypatch.setattr(solar_feedback, "first_retained_state", lambda *_args: observations[0])
        hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity: observations[-1]))
        request = {
            "entity_id": entity,
            "configuration_version": "c" * 64,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        first = await solar_feedback.capture(hass, request)
        assert first["start"] == start.isoformat()
        assert first["configuration_version"] == "c" * 64
        assert first["feedback"]["entity_id"] == entity
        assert query_sizes == [10001]
        assert len(first["feedback"]["readings"]) == 10000
        cutoff = observations[10000].last_updated.isoformat()
        assert first["feedback"]["checked_until"] == cutoff
        second = await solar_feedback.capture(hass, {**request, "start": cutoff})
        assert second["feedback"]["checked_until"] == end.isoformat()
        combined = first["feedback"]["readings"] + second["feedback"]["readings"]
        assert combined == [
            [row.last_updated.isoformat(), (False, True, None)[index % 3]]
            for index, row in enumerate(observations)
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_name", ["AuthenticationError", "ReleaseMismatchError"])
def test_actionable_feedback_failure_is_consumed_once_and_reconnect_recovers(
    monkeypatch, failure_name
):
    """Report each connection guard once, then allow a successful reconnect to schedule another read."""
    from custom_components.open_energy_planner import solar_feedback

    async def scenario():
        """Keep the failed background result until the normal coordinator recovery boundary consumes it."""
        failure = getattr(solar_feedback, failure_name)
        now = dt_util.utcnow()
        request = {
            "entity_id": "binary_sensor.roof",
            "configuration_version": "c" * 64,
            "start": (now - timedelta(days=1)).isoformat(),
            "end": now.isoformat(),
        }
        reads = []

        async def capture(_hass, _request, **_options):
            """The second actual request represents restored credentials/release compatibility."""
            reads.append(1)
            if len(reads) == 1:
                raise failure("private-connection-sentinel")
            return {"accepted": True}

        async def send(_method, _path, _body):
            """Accept a normalized response after successful reconnect."""

        monkeypatch.setattr(solar_feedback, "capture", capture)
        coordinator = SimpleNamespace(
            closed=False,
            solar_feedback_task=None,
            solar_feedback_attempts={},
            solar_feedback_reads={},
            client=SimpleNamespace(request=send),
            hass=SimpleNamespace(
                async_create_background_task=lambda task, _name: asyncio.create_task(task)
            ),
        )
        snapshot = {"solar": {"feedback_requests": [request]}}
        await solar_feedback.synchronize(coordinator, snapshot)
        with pytest.raises(failure):
            await coordinator.solar_feedback_task
        with pytest.raises(failure):
            await solar_feedback.synchronize(coordinator, snapshot)
        assert coordinator.solar_feedback_task is None
        monkeypatch.setattr(solar_feedback.dt_util, "utcnow", lambda: now + timedelta(minutes=6))
        await solar_feedback.synchronize(coordinator, snapshot)
        await coordinator.solar_feedback_task
        assert len(reads) == 2

    asyncio.run(scenario())


def test_timed_out_native_query_does_not_stack_or_starve_another_sensor(monkeypatch):
    """A timed-out native executor retains single ownership until completion while another sensor progresses."""
    from custom_components.open_energy_planner import solar_feedback

    async def scenario():
        """Keep the native worker alive independently of cancellation of its asyncio waiter."""
        now = dt_util.utcnow()
        worker = asyncio.Future()
        queries, delivered = [], []

        async def committed():
            """There are no outstanding writes in this query-ownership fixture."""

        def execute(query):
            """A cancelled await cannot stop the simulated database thread."""
            entity = query.args[3]
            entity = entity[0] if isinstance(entity, list) else entity
            queries.append(entity)
            if entity.endswith("broken"):
                return asyncio.shield(worker)
            result = asyncio.Future()
            result.set_result({})
            return result

        async def send(_method, _path, body):
            """Observe the healthy source's actual completed response."""
            delivered.append(body["feedback"]["entity_id"])

        monkeypatch.setattr(solar_feedback, "FEEDBACK_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(
            solar_feedback,
            "get_instance",
            lambda _hass: SimpleNamespace(
                async_block_till_done=committed, async_add_executor_job=execute
            ),
        )
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda _entity: None),
            async_create_background_task=lambda task, _name: asyncio.create_task(task),
        )
        coordinator = SimpleNamespace(
            closed=False,
            solar_feedback_task=None,
            solar_feedback_attempts={},
            solar_feedback_reads={},
            hass=hass,
            client=SimpleNamespace(request=send),
        )
        snapshot = {
            "solar": {
                "feedback_requests": [
                    {
                        "entity_id": f"binary_sensor.{name}",
                        "configuration_version": "c" * 64,
                        "start": (now - timedelta(days=1)).isoformat(),
                        "end": now.isoformat(),
                    }
                    for name in ("broken", "healthy")
                ]
            }
        }
        try:
            await solar_feedback.synchronize(coordinator, snapshot)
            await coordinator.solar_feedback_task
            assert not worker.done()
            monkeypatch.setattr(
                solar_feedback.dt_util, "utcnow", lambda: now + timedelta(minutes=6)
            )
            await solar_feedback.synchronize(coordinator, snapshot)
            await coordinator.solar_feedback_task
            assert queries == ["binary_sensor.broken", "binary_sensor.healthy"]
            assert delivered == ["binary_sensor.healthy"]
        finally:
            if not worker.done():
                worker.set_result({})
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_real_recorder_queued_transitions_commit_before_checked_coverage(monkeypatch):
    """A blocked Recorder queue cannot hide an intermediate on/off sequence behind its current off state."""
    from homeassistant.components.recorder.tasks import RecorderTask

    from custom_components.open_energy_planner.solar_feedback import capture

    async def scenario():
        """Hold real SQLite persistence, enqueue actual HA events and release only at the commit barrier."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            recorder = get_instance(hass)
            entity = "binary_sensor.synthetic_curtailment"
            hass.states.async_set(entity, "off")
            initial = hass.states.get(entity)
            await hass.async_block_till_done()
            await recorder.async_block_till_done()
            entered, release = Event(), Event()

            class HoldPersistence(RecorderTask):
                """Pause the actual Recorder thread before subsequently queued state changes."""

                def run(self, _instance):
                    """Keep a bounded emergency release so fixture cleanup cannot strand the thread."""
                    entered.set()
                    assert release.wait(10), "Recorder test barrier was not released"

            recorder.queue_task(HoldPersistence())
            assert await hass.async_add_executor_job(entered.wait, 5)
            observations = [initial]
            for value in ("on", "unavailable", "off"):
                hass.states.async_set(entity, value)
                observations.append(hass.states.get(entity))
            await hass.async_block_till_done()
            original_barrier = recorder.async_block_till_done

            async def finish_queued_writes():
                """Release the paused thread only when capture requests Recorder's supported commit barrier."""
                release.set()
                await original_barrier()

            try:
                with monkeypatch.context() as patch:
                    patch.setattr(recorder, "async_block_till_done", finish_queued_writes)
                    end = dt_util.utcnow()
                    response = await capture(
                        hass,
                        {
                            "entity_id": entity,
                            "configuration_version": "c" * 64,
                            "start": (initial.last_updated - timedelta(seconds=1)).isoformat(),
                            "end": end.isoformat(),
                        },
                    )
                    assert response["feedback"]["checked_until"] == end.isoformat()
                    assert response["feedback"]["readings"] == [
                        [row.last_updated.isoformat(), value]
                        for row, value in zip(observations, (False, True, None, False), strict=True)
                    ]
            finally:
                release.set()
                await original_barrier()

    asyncio.run(scenario())


def test_feedback_commit_and_query_share_one_total_deadline(monkeypatch):
    """A stalled persistence barrier times out without querying or claiming checked coverage."""
    from custom_components.open_energy_planner import solar_feedback

    async def scenario():
        """Use a shortened test deadline while leaving the production twenty-second policy unchanged."""
        cancelled = asyncio.Event()

        async def stalled_commit():
            """Never commit and prove cancellation drains the pending barrier."""
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def unexpected_query(_query):
            """A timed-out barrier must never begin the query phase."""
            pytest.fail("Recorder was queried before the queued writes committed")

        monkeypatch.setattr(solar_feedback, "FEEDBACK_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(
            solar_feedback,
            "get_instance",
            lambda _hass: SimpleNamespace(
                async_block_till_done=stalled_commit, async_add_executor_job=unexpected_query
            ),
        )
        now = dt_util.utcnow()
        with pytest.raises(TimeoutError):
            await solar_feedback.capture(
                SimpleNamespace(),
                {
                    "entity_id": "binary_sensor.synthetic_curtailment",
                    "configuration_version": "c" * 64,
                    "start": (now - timedelta(hours=1)).isoformat(),
                    "end": now.isoformat(),
                },
            )
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_real_recorder_retains_feedback_times_and_unknown_values():
    """Read actual off/on/unavailable transitions and preserve their original timestamps through the adapter."""
    from custom_components.open_energy_planner.solar_feedback import capture

    async def scenario():
        """Use the pinned HA Recorder and its executor with an isolated synthetic sensor."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            start = dt_util.utcnow() - timedelta(seconds=1)
            entity = "binary_sensor.synthetic_curtailment"
            observations = []
            for value in ("off", "on", "unavailable", "off"):
                hass.states.async_set(entity, value)
                observations.append(hass.states.get(entity))
                await hass.async_block_till_done()
                await get_instance(hass).async_block_till_done()
            end = dt_util.utcnow()
            request = {
                "entity_id": entity,
                "configuration_version": "c" * 64,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
            response = await capture(hass, request)
            assert response["configuration_version"] == "c" * 64
            assert response["start"] == request["start"]
            assert response["feedback"] == {
                "entity_id": entity,
                "checked_until": end.isoformat(),
                "readings": [
                    [state.last_updated.isoformat(), value]
                    for state, value in zip(observations, (False, True, None, False), strict=True)
                ],
            }
            resumed = await capture(
                hass, {**request, "start": observations[1].last_updated.isoformat()}
            )
            assert resumed["feedback"]["readings"] == response["feedback"]["readings"][1:]

    asyncio.run(scenario())


def test_real_recorder_native_row_limit_preserves_boundary_transition(monkeypatch):
    """A native three-row query yields two transferred observations and resumes at the exact third transition."""
    from custom_components.open_energy_planner import solar_feedback

    async def scenario():
        """Use actual Recorder storage and a deliberately small transfer budget to saturate its SQL limit."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            entity = "binary_sensor.synthetic_curtailment"
            observations = []
            for value in ("off", "on", "unavailable", "off"):
                hass.states.async_set(entity, value)
                observations.append(hass.states.get(entity))
                await hass.async_block_till_done()
                await get_instance(hass).async_block_till_done()
            original = solar_feedback.state_changes_during_period
            sizes = []

            def read(*args, **options):
                """Observe the actual native SQL-limited result, rather than truncating fixture rows."""
                assert options["limit"] == 3
                result = original(*args, **options)
                sizes.append(len(result.get(entity, ())))
                return result

            monkeypatch.setattr(solar_feedback, "MAX_FEEDBACK_READINGS", 2)
            monkeypatch.setattr(solar_feedback, "state_changes_during_period", read)
            request = {
                "entity_id": entity,
                "configuration_version": "c" * 64,
                "start": (observations[0].last_updated - timedelta(seconds=1)).isoformat(),
                "end": dt_util.utcnow().isoformat(),
            }
            first = await solar_feedback.capture(hass, request)
            assert first["feedback"]["checked_until"] == observations[2].last_updated.isoformat()
            second = await solar_feedback.capture(
                hass, {**request, "start": first["feedback"]["checked_until"]}
            )
            assert sizes == [3, 2]
            assert first["feedback"]["readings"] + second["feedback"]["readings"] == [
                [row.last_updated.isoformat(), value]
                for row, value in zip(observations, (False, True, None, False), strict=True)
            ]

    asyncio.run(scenario())


def test_background_feedback_failure_does_not_block_another_source(monkeypatch):
    """Failed reads retry independently while a second sensor can deliver evidence without blocking synchronization."""
    from custom_components.open_energy_planner import solar_feedback

    async def scenario():
        """Keep one background read pending and prove its caller returns and the next source still runs."""
        now = dt_util.utcnow()
        requests = [
            {
                "entity_id": f"binary_sensor.{name}",
                "configuration_version": "c" * 64,
                "start": (now - timedelta(days=1)).isoformat(),
                "end": now.isoformat(),
            }
            for name in ("broken", "usable")
        ]
        entered, release = asyncio.Event(), asyncio.Event()
        received = []

        async def read(_hass, request, **_options):
            """Hold the first controlled failure until after the scheduler has returned."""
            if request["entity_id"].endswith("broken"):
                entered.set()
                await release.wait()
                raise OSError("private-provider-error")
            return {"source": request["entity_id"]}

        async def send(method, path, body):
            """Record only successful accepted normalized transport."""
            assert (method, path) == ("POST", "/v1/solar/feedback")
            received.append(body)

        monkeypatch.setattr(solar_feedback, "capture", read)
        coordinator = SimpleNamespace(
            closed=False,
            data={"solar": {"feedback_requests": requests}},
            solar_feedback_task=None,
            solar_feedback_attempts={},
            solar_feedback_reads={},
            client=SimpleNamespace(request=send),
            hass=SimpleNamespace(
                async_create_background_task=lambda coroutine, _name: asyncio.create_task(coroutine)
            ),
        )
        await solar_feedback.synchronize(coordinator, coordinator.data)
        await entered.wait()
        assert not coordinator.solar_feedback_task.done()
        release.set()
        await coordinator.solar_feedback_task
        await solar_feedback.synchronize(coordinator, coordinator.data)
        await coordinator.solar_feedback_task
        assert received == [{"source": "binary_sensor.usable"}]

    asyncio.run(scenario())
