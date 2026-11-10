"""Transport real Recorder feedback without converting unknown states to unrestricted production."""

import asyncio
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.util import dt as dt_util

from tests.custom_component.support.chain import running_chain


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

        async def committed():
            """This isolated query fixture has no queued transactions."""

        async def execute(query):
            """Execute the supplied Recorder query without another thread in this transport fixture."""
            return query()

        def history(_hass, lower, upper, entities, **_options):
            """Match pinned Recorder's strict SQL boundaries, including the next chunk's exact-start trap."""
            assert entities == [entity]
            return {entity: [row for row in observations if lower < row.last_updated < upper]}

        recorder = SimpleNamespace(async_block_till_done=committed, async_add_executor_job=execute)
        monkeypatch.setattr(solar_feedback, "get_instance", lambda _hass: recorder)
        monkeypatch.setattr(solar_feedback, "get_significant_states", history)
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

        async def read(_hass, request):
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
