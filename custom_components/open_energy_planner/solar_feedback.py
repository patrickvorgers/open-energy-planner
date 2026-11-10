"""Read observed curtailment states without executing or inferring physical commands."""

import asyncio
import logging
from datetime import datetime
from functools import partial

from aiohttp import ClientError
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.util import dt as dt_util

from .client import AuthenticationError, ReleaseMismatchError

MAX_FEEDBACK_READINGS = 10000
MAX_FEEDBACK_RANGE_DAYS = 367
FEEDBACK_TIMEOUT_SECONDS = 20
FEEDBACK_RETRY_SECONDS = 300
_LOGGER = logging.getLogger(__name__)


def feedback_value(value):
    """Admit only explicit on/off or boolean-like sensor states. Unknown stays unknown."""
    if value in ("on", "true", "1"):
        return True
    if value in ("off", "false", "0"):
        return False
    return None


async def capture(hass, request):
    """Read one indexed sensor history increment, preserving actual observation timestamps."""
    start, end = (datetime.fromisoformat(request[key]) for key in ("start", "end"))
    entity = request["entity_id"]
    if (
        not entity.startswith(("binary_sensor.", "sensor."))
        or not 0 < (end - start).total_seconds() <= MAX_FEEDBACK_RANGE_DAYS * 24 * 3600
    ):
        raise ValueError("Invalid curtailment feedback request")
    states = await asyncio.wait_for(
        get_instance(hass).async_add_executor_job(
            partial(
                get_significant_states,
                hass,
                start,
                end,
                [entity],
                include_start_time_state=False,
                significant_changes_only=False,
            )
        ),
        FEEDBACK_TIMEOUT_SECONDS,
    )
    observations = list(states.get(entity, ()))
    current = hass.states.get(entity)
    if current is not None:
        observations.append(current)
    readings = {}
    for state in observations:
        at = state.last_updated
        if start <= at < end:
            value = feedback_value(state.state)
            if at in readings and readings[at] != value:
                value = None
            readings[at] = value
    if len(readings) > MAX_FEEDBACK_READINGS:
        raise ValueError("Curtailment feedback range exceeds transfer limit")
    return {
        "configuration_version": request["configuration_version"],
        "start": request["start"],
        "feedback": {
            "entity_id": entity,
            "checked_until": request["end"],
            "readings": [[at.isoformat(), value] for at, value in sorted(readings.items())],
        },
    }


async def synchronize(coordinator, snapshot):
    """Keep Recorder reads off the command synchronization path and retry failures at a bounded rate."""
    previous = coordinator.solar_feedback_task
    if previous and not previous.done():
        return
    if previous and not previous.cancelled():
        previous.result()
    now = dt_util.utcnow()
    request = next(
        (
            row
            for row in snapshot.get("solar", {}).get("feedback_requests", ())
            if row["entity_id"] not in coordinator.solar_feedback_attempts
            or coordinator.solar_feedback_attempts[row["entity_id"]][0] != row["start"]
            or (now - coordinator.solar_feedback_attempts[row["entity_id"]][1]).total_seconds()
            >= FEEDBACK_RETRY_SECONDS
        ),
        None,
    )
    if coordinator.closed or not request:
        return
    coordinator.solar_feedback_attempts[request["entity_id"]] = (request["start"], now)

    async def read():
        """Send only normalized evidence. A failed read never extends checked coverage."""
        try:
            body = await capture(coordinator.hass, request)
            await coordinator.client.request("POST", "/v1/solar/feedback", body)
        except (AuthenticationError, ReleaseMismatchError):
            raise
        except (ClientError, OSError, RuntimeError, ValueError, TypeError, KeyError):
            _LOGGER.debug(
                "Solar curtailment feedback could not be refreshed. Previously checked history is retained. Retrying in five minutes."
            )

    coordinator.solar_feedback_task = coordinator.hass.async_create_background_task(
        read(), "OEP solar curtailment feedback"
    )
