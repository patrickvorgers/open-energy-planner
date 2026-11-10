"""Read observed curtailment states without executing or inferring physical commands."""

import asyncio
import logging
from datetime import datetime, timedelta
from functools import partial
from math import inf, nextafter

from aiohttp import ClientError
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import States
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import State
from homeassistant.util import dt as dt_util
from sqlalchemy import select

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


def recorder_boundary(instant):
    """Find the first native float timestamp that rounds to this microsecond or later."""
    # Recorder floats can round up to the microsecond cursor. Match that same datetime boundary.
    lower = instant.timestamp() - 0.5e-6
    while dt_util.utc_from_timestamp(lower) < instant:
        lower = nextafter(lower, inf)
    while dt_util.utc_from_timestamp(previous := nextafter(lower, -inf)) >= instant:
        lower = previous
    return lower


def retained_feedback_bounds(hass, start, end, entity):
    """Read indexed start coverage and the first original observation in two bounded queries."""
    with session_scope(hass=hass, read_only=True) as session:
        metadata_id = get_instance(hass).states_meta_manager.get(entity, session, False)
        if metadata_id is None:
            return None, False
        covered = (
            session.execute(
                select(States.state_id)
                .where(
                    States.metadata_id == metadata_id,
                    States.last_updated_ts < recorder_boundary(start + timedelta(microseconds=1)),
                )
                .order_by(States.last_updated_ts.desc())
                .limit(1)
            ).first()
            is not None
        )
        row = session.execute(
            select(States.state, States.last_updated_ts)
            .where(
                States.metadata_id == metadata_id,
                States.last_updated_ts >= recorder_boundary(start),
                States.last_updated_ts < recorder_boundary(end),
            )
            .order_by(States.last_updated_ts)
            .limit(1)
        ).first()
        if row is None:
            return None, covered
        return (
            State(
                entity,
                row.state or "unknown",
                last_updated=dt_util.utc_from_timestamp(row.last_updated_ts),
            ),
            covered,
        )


def recorder_observations(hass, start, end, entity):
    """Keep the bounded first raw observation and native transitions under one executor owner."""
    first, covered = retained_feedback_bounds(hass, start, end, entity)
    states = state_changes_during_period(
        hass,
        # Recorder uses a strict lower SQL bound. Retain an exact cursor observation.
        start - timedelta(microseconds=1),
        end,
        entity,
        no_attributes=True,
        limit=MAX_FEEDBACK_READINGS + 1,
        include_start_time_state=False,
    )
    if first is not None:
        states.setdefault(entity, []).append(first)
    return states, covered


async def capture(hass, request, *, pending_reads=None):
    """Read one indexed sensor history increment, preserving actual observation timestamps."""
    start, end = (datetime.fromisoformat(request[key]) for key in ("start", "end"))
    entity = request["entity_id"]
    if (
        not entity.startswith(("binary_sensor.", "sensor."))
        or not 0 < (end - start).total_seconds() <= MAX_FEEDBACK_RANGE_DAYS * 24 * 3600
    ):
        raise ValueError("Invalid curtailment feedback request")

    pending_reads = pending_reads if pending_reads is not None else {}
    if entity in pending_reads and not pending_reads[entity].done():
        raise RuntimeError("Solar feedback Recorder read is still running")

    def completed(query):
        """Release native-work ownership only when the executor really finishes, even after timeout."""
        if not query.cancelled():
            query.exception()
        if pending_reads.get(entity) is query:
            pending_reads.pop(entity)

    recorder = get_instance(hass)
    async with asyncio.timeout(FEEDBACK_TIMEOUT_SECONDS):
        await recorder.async_block_till_done()
        query = asyncio.ensure_future(
            recorder.async_add_executor_job(
                partial(
                    recorder_observations,
                    hass,
                    start,
                    end,
                    entity,
                )
            )
        )
        pending_reads[entity] = query
        query.add_done_callback(completed)
        states, covered = await asyncio.shield(query)
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
    if not covered and start not in readings:
        readings[start] = None
    ordered = sorted(readings.items())
    if len(ordered) > MAX_FEEDBACK_READINGS:
        end = ordered[MAX_FEEDBACK_READINGS][0]
        ordered = ordered[:MAX_FEEDBACK_READINGS]
    return {
        "configuration_version": request["configuration_version"],
        "start": request["start"],
        "feedback": {
            "entity_id": entity,
            "checked_until": end.isoformat(),
            "readings": [[at.isoformat(), value] for at, value in ordered],
        },
    }


async def synchronize(coordinator, snapshot):
    """Keep Recorder reads off the command synchronization path and retry failures at a bounded rate."""
    previous = coordinator.solar_feedback_task
    if coordinator.closed or previous and not previous.done():
        return
    coordinator.solar_feedback_task = None
    if previous and not previous.cancelled():
        previous.result()
    now = dt_util.utcnow()
    request = next(
        (
            row
            for row in snapshot.get("solar", {}).get("feedback_requests", ())
            if row["entity_id"] not in coordinator.solar_feedback_reads
            and (
                row["entity_id"] not in coordinator.solar_feedback_attempts
                or coordinator.solar_feedback_attempts[row["entity_id"]][0] != row["start"]
                or (now - coordinator.solar_feedback_attempts[row["entity_id"]][1]).total_seconds()
                >= FEEDBACK_RETRY_SECONDS
            )
        ),
        None,
    )
    if coordinator.closed or not request:
        return
    coordinator.solar_feedback_attempts[request["entity_id"]] = (request["start"], now)

    async def read():
        """Send only normalized evidence. A failed read never extends checked coverage."""
        try:
            body = await capture(
                coordinator.hass, request, pending_reads=coordinator.solar_feedback_reads
            )
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
