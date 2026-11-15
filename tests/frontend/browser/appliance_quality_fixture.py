"""Persist genuinely followed synthetic cycles before browser startup, exercising restart retention."""

from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.cycles.service import ApplianceService
from open_energy_planner.appliances.forecasting import ConsumerForecasts
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.appliance.raw_support import follow_raw_trace, raw_trace
from tests.app.appliance.support import ENTITY, POWER


def seed(directory, *, representative=False, statistical=False):
    """Follow two real SQL-backed traces with heartbeats, then close storage before the browser app restarts."""
    with closing(SqlPlanRepository(directory)) as repository:
        consumers = ConsumerForecasts(SqlDocuments(repository.engine, "demand"))
        entities = (ENTITY,) if representative else (ENTITY, ENTITY + "_available")
        consumers.sync(
            {
                "entities": [{"entity_id": entity, "name": entity} for entity in entities],
                "suggestions": {
                    "consumers": [
                        {"entity_id": entity, "power_entity": POWER, "upstream": None}
                        for entity in entities
                    ]
                },
            }
        )
        consumers.save(
            [{"entity_id": entity, "weather_sensitive": False} for entity in entities],
            consumers.version,
        )
        service = ApplianceService(SqlDocuments(repository.engine, "planning"), consumers)
        body = representative_trace() if representative else raw_trace()
        anchor = datetime.now(UTC)
        if representative:
            anchor = anchor.replace(
                minute=anchor.minute // 5 * 5, second=0, microsecond=0
            ) - timedelta(minutes=5)
        else:
            anchor -= timedelta(seconds=1)
        offset = anchor - instant(body["end"])
        for field in ("start", "end", "power_reported_at", "energy_reported_at"):
            body[field] = (instant(body[field]) + offset).isoformat()
        for field in ("power_states", "energy_states"):
            for row in body[field]:
                row["at"] = (instant(row["at"]) + offset).isoformat()
        for index, entity in enumerate(entities):
            trace = deepcopy(body)
            trace["entity_id"] = entity
            for row in trace["power_states"]:
                row["state"] = str(float(row["state"]) * (index + 1))
            for row in trace["energy_states"]:
                row["state"] = str(10 + (float(row["state"]) - 10) * (index + 1))
            if statistical:
                first, total, rows = instant(trace["start"]) - timedelta(minutes=5), 10, []
                for index in range(31):
                    at = first + timedelta(minutes=5 * index)
                    watts = next(
                        (
                            float(row["state"])
                            for row in reversed(trace["power_states"])
                            if instant(row["at"]) <= at
                        ),
                        0,
                    )
                    total += watts / 12000
                    rows.append({"at": at.isoformat(), "power_w": watts, "kwh": round(total, 1)})
                service.learn_history(entity, POWER, rows, True, instant(trace["end"]))
                service.learn_raw_history(trace, instant(trace["end"]))
            else:
                follow_raw_trace(service, trace)


def representative_trace():
    """Keep a complete ninety-minute synthetic wash, internal pause and twenty-second heater pulse."""
    body = raw_trace()
    started = instant(body["power_states"][1]["at"])
    watts = [120, 2100, 2080, 2060, 150, 120, 90, 130, 0, 0, 0, 1600, 1750, 1400, 90, 60, 30, 20]
    states = (
        [(-1800, 0)]
        + [(index * 300, power) for index, power in enumerate(watts)]
        + [(5400, 0), (7200, 0)]
    )
    states += [(450, 3000), (470, 2100)]
    states.sort()
    energy, previous, previous_power = 10.0, -1800, 0
    powers, counters = [], []
    for second, power in states:
        energy += previous_power * (second - previous) / 3600000
        at = (started + timedelta(seconds=second)).isoformat()
        powers.append({"at": at, "state": str(power), "unit": "W"})
        counters.append({"at": at, "state": str(energy), "unit": "kWh"})
        previous, previous_power = second, power
    body.update(
        power_states=powers,
        energy_states=counters,
        end=(started + timedelta(seconds=7200)).isoformat(),
    )
    body["power_reported_at"] = body["energy_reported_at"] = body["end"]
    return body
