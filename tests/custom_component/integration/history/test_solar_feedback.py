"""Transport real Recorder feedback without converting unknown states to unrestricted production."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from homeassistant.components.recorder import get_instance
from homeassistant.util import dt as dt_util

from tests.custom_component.support.chain import running_chain


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
