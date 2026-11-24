"""Exercise selected measurement events and bounded coherent reporting."""

import asyncio
from contextlib import suppress

from custom_components.open_energy_planner import observation_push
from custom_components.open_energy_planner.observation_bridge import ObservationBridge


class SensorEvents:
    """Track a synthetic indexed listener and deliver only matching state changes."""

    def __init__(self):
        """Retain active listeners and their installation/removal history."""
        self.listeners = {}
        self.subscribed = []
        self.removed = []

    def subscribe(self, entities, notify):
        """Record the exact indexed selection and provide its removal callback."""
        self.listeners[entities] = notify
        self.subscribed.append(entities)

        def remove():
            """Remove this listener and record its former entity set."""
            self.removed.append(entities)
            del self.listeners[entities]

        return remove

    def change(self, entity_id):
        """Deliver a synthetic event to the same selection a HA indexed listener would use."""
        for entities, notify in self.listeners.items():
            if entity_id in entities:
                notify()


def bridge(revision, *entities, maximum_age_s=300):
    """Create real source bindings with independently chosen subscription entities."""
    return ObservationBridge(
        None,
        {
            "revision": revision,
            "bindings": [
                {
                    "key": f"grid-{index}",
                    "entity_id": entity_id,
                    "quantity": "power",
                    "multiplier": 1,
                    "required": True,
                    "maximum_age_s": maximum_age_s,
                    "timestamp_attribute": None,
                }
                for index, entity_id in enumerate(entities)
            ],
        },
    )


def test_selected_events_are_coalesced_and_listener_is_replaced(monkeypatch):
    """Capture on install, coalesce dirty selected sensors and remove the old selection."""

    async def scenario():
        """Drive selected events without Home Assistant or real sensor data."""
        monkeypatch.setattr(observation_push, "MIN_PUSH_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(observation_push, "COALESCE_SECONDS", 0.001)
        events = SensorEvents()
        reports = asyncio.Queue()
        current = bridge("sources-1", "sensor.grid", "sensor.grid", "sensor.pv")

        async def send():
            """Report the current revision and measurement values at capture time."""
            reports.put_nowait(current.revision)
            return True

        push = observation_push.ObservationPush(events.subscribe, send)
        task = asyncio.create_task(push.run())
        try:
            await asyncio.sleep(0)
            assert reports.empty()
            push.configure(current)
            assert await asyncio.wait_for(reports.get(), timeout=1) == "sources-1"
            assert events.subscribed == [("sensor.grid", "sensor.pv")]
            events.change("sensor.unselected")
            await asyncio.sleep(0.01)
            assert reports.empty()
            events.change("sensor.grid")
            events.change("sensor.pv")
            events.change("sensor.grid")
            assert await asyncio.wait_for(reports.get(), timeout=1) == "sources-1"
            await asyncio.sleep(0.01)
            assert reports.empty()

            current = bridge("sources-2", "sensor.grid", "sensor.pv")
            push.configure(current)
            assert await asyncio.wait_for(reports.get(), timeout=1) == "sources-2"
            assert events.subscribed == [("sensor.grid", "sensor.pv")]
            current = bridge("sources-3", "sensor.battery")
            push.configure(current)
            assert await asyncio.wait_for(reports.get(), timeout=1) == "sources-3"
            assert events.subscribed[-1] == ("sensor.battery",)
            assert events.removed == [("sensor.grid", "sensor.pv")]
            events.change("sensor.pv")
            await asyncio.sleep(0.01)
            assert reports.empty()
            push.configure(None)
            assert not events.listeners
            events.change("sensor.battery")
            await asyncio.sleep(0.01)
            assert reports.empty()
        finally:
            push.close()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_unchanged_state_refreshes_and_failed_delivery_retries(monkeypatch):
    """An unchanged source gets a timely heartbeat and a rejected report gets an early retry."""

    async def scenario():
        """Use short test intervals to cover both deadline branches."""
        monkeypatch.setattr(observation_push, "HEARTBEAT_SECONDS", 5)
        monkeypatch.setattr(observation_push, "MIN_PUSH_INTERVAL_SECONDS", 0.01)
        events = SensorEvents()
        reports = asyncio.Queue()
        attempts = 0

        async def send():
            """Fail the first delivery and accept all subsequent captures."""
            nonlocal attempts
            attempts += 1
            reports.put_nowait(attempts)
            return attempts > 1

        push = observation_push.ObservationPush(events.subscribe, send)
        push.configure(bridge("sources-1", "sensor.grid", maximum_age_s=0.1))
        task = asyncio.create_task(push.run())
        try:
            assert await asyncio.wait_for(reports.get(), timeout=1) == 1
            assert await asyncio.wait_for(reports.get(), timeout=1) == 2
            assert await asyncio.wait_for(reports.get(), timeout=1) == 3
        finally:
            push.close()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        assert not events.listeners

    asyncio.run(scenario())


def test_binding_change_during_transfer_sends_current_revision_without_overlapping():
    """A delayed old report cannot inhibit a coherent report for the replacement bindings."""

    async def scenario():
        """Hold the original transfer while selection changes several times."""
        events = SensorEvents()
        reports = asyncio.Queue()
        release = asyncio.Event()
        current = bridge("sources-1", "sensor.grid")

        async def send():
            """Hold the first report long enough to supersede its captured revision."""
            reports.put_nowait(current.revision)
            if current.revision == "sources-1":
                await release.wait()
                return False
            return True

        push = observation_push.ObservationPush(events.subscribe, send)
        push.configure(current)
        task = asyncio.create_task(push.run())
        try:
            assert await asyncio.wait_for(reports.get(), timeout=1) == "sources-1"
            current = bridge("sources-2", "sensor.pv")
            push.configure(current)
            current = bridge("sources-3", "sensor.battery")
            push.configure(current)
            events.change("sensor.battery")
            await asyncio.sleep(0)
            assert reports.empty()
            release.set()
            assert await asyncio.wait_for(reports.get(), timeout=1) == "sources-3"
            assert not reports.qsize()
            assert list(events.listeners) == [("sensor.battery",)]
        finally:
            push.close()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        assert not events.listeners

    asyncio.run(scenario())
