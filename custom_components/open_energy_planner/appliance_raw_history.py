"""Collect bounded original Recorder states while retaining physical executor ownership after timeout."""

import asyncio
import json
import logging
from datetime import timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_last_state_changes
from homeassistant.util import dt as dt_util
from sqlalchemy.exc import SQLAlchemyError

MAX_RAW_STATES = 2048
MAX_RAW_BYTES = 512 * 1024
RAW_WINDOW_HOURS = 14
RAW_QUERY_TIMEOUT_SECONDS = 20
_LOGGER = logging.getLogger(__name__)


def consume(future):
    """Drain completed executor failures without cancelling the underlying Recorder query."""
    if not future.cancelled():
        future.exception()


async def capture(coordinator, entity, power_entity, synthetic):
    """Read at most N+1 native states per selected sensor, with no unbounded pagination.

    HA's native query applies a SQL row limit before materializing attributes.
    It retains attribute-only state updates and original timestamps. A carry-in
    state stays at its original time, never a synthetic fresh sample. A limited
    tail does not prove coverage of the entire requested window.

    Shield the retained executor future. Cancelling/timing out this awaiter must
    not make a still-running query look finished to the next history pass.
    """
    if coordinator.closed or "recorder" not in coordinator.hass.config.components:
        return None
    previous = getattr(coordinator, "appliance_raw_history_future", None)
    if previous is not None and not previous.done():
        return None
    if previous is not None:
        consume(previous)

    def read():
        """Use the native row-limited API rather than materializing an unbounded time-range response."""
        result = {}
        for identity in (entity, power_entity):
            result[identity] = [
                {
                    "at": state.last_updated.isoformat(),
                    "state": state.state,
                    "unit": state.attributes.get("unit_of_measurement"),
                }
                for state in get_last_state_changes(
                    coordinator.hass, MAX_RAW_STATES + 1, identity
                ).get(identity, [])
            ]
        return result

    future = get_instance(coordinator.hass).async_add_executor_job(read)
    coordinator.appliance_raw_history_future = future
    future.add_done_callback(consume)
    try:
        result = await asyncio.wait_for(asyncio.shield(future), RAW_QUERY_TIMEOUT_SECONDS)
    except TimeoutError:
        return None
    except SQLAlchemyError:
        _LOGGER.debug(
            "Raw appliance Recorder query unavailable. Retaining statistics and continuing historical scan. Retrying on the next history refresh"
        )
        return None
    if coordinator.closed:
        return None
    end = dt_util.utcnow()
    start = end - timedelta(hours=RAW_WINDOW_HOURS)
    limited = False

    def selected(identity):
        """Keep only the actual bounded tail and at most one original carry-in before the requested start."""
        nonlocal limited
        rows = sorted(result[identity], key=lambda row: row["at"])
        limited |= len(rows) > MAX_RAW_STATES
        before = [row for row in rows if dt_util.parse_datetime(row["at"]) < start]
        current = [row for row in rows if start <= dt_util.parse_datetime(row["at"]) <= end]
        return before[-1:] + current

    energy_states, power_states = selected(entity), selected(power_entity)

    def reported(identity, rows):
        """Freshness belongs to a genuine current HA report matching the last recorded state and unit."""
        current = coordinator.hass.states.get(identity)
        if (
            not rows
            or current is None
            or current.state != rows[-1]["state"]
            or current.attributes.get("unit_of_measurement") != rows[-1]["unit"]
        ):
            return None
        return current.last_reported.isoformat()

    body = {
        "observer_session": coordinator.appliance_observer_session,
        "entity_id": entity,
        "power_entity": power_entity,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "energy_states": energy_states,
        "power_states": power_states,
        "energy_reported_at": reported(entity, energy_states),
        "power_reported_at": reported(power_entity, power_states),
        "limited": limited,
        "synthetic": synthetic,
    }
    if len(json.dumps(body, allow_nan=False).encode()) > MAX_RAW_BYTES:
        return None
    return body
