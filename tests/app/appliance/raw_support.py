"""Independent irregular Recorder traces with asynchronous counter anchors and a twenty-second peak."""

from datetime import timedelta

from tests.app.appliance.support import ENTITY, NOW, POWER


def raw_trace(*, completed=True, conflict=False):
    """A thirty-minute 1-kW run plus a 20-second 2-kW excess consumes exactly 23/45 kWh."""
    start = NOW - timedelta(hours=2)
    powers = [(-1800, 0), (0, 1000), (600.3, 3000), (620.3, 1000)]
    if completed:
        powers += [(1800, 0), (3600, 0)]
    end = start + timedelta(seconds=3600 if completed else 900)

    def stamp(seconds):
        """Preserve genuine event times rather than placing values on an invented grid."""
        return (start + timedelta(seconds=seconds)).isoformat()

    counters = [(-20, 10), (600.1, 10 + 600.1 / 3600), (620.6, 10 + 620.6 / 3600 + 1 / 90)]
    if completed:
        counters += [(1801, 10 + 23 / 45), (3600, 10 + 23 / 45)]
    else:
        counters += [(900, 10 + 0.25 + 1 / 90)]
    if conflict:
        counters = [(at, 10 + (value - 10) * 2) for at, value in counters]
    return {
        "observer_session": "synthetic-live-observer",
        "entity_id": ENTITY,
        "power_entity": POWER,
        "start": stamp(-1800),
        "end": end.isoformat(),
        "power_states": [
            {"at": stamp(at), "state": str(value), "unit": "W"} for at, value in powers
        ],
        "energy_states": [
            {"at": stamp(at), "state": str(value), "unit": "kWh"} for at, value in counters
        ],
        "power_reported_at": end.isoformat(),
        "energy_reported_at": end.isoformat(),
        "limited": False,
        "synthetic": True,
    }


def dense_raw_trace():
    """Retain frequent genuine quiet-state reports without changing the 3-kW pulse or 23/45-kWh cycle."""
    body = raw_trace()
    first = NOW - timedelta(hours=2, minutes=30)
    for field, value, unit in (("power_states", 0, "W"), ("energy_states", 10, "kWh")):
        body[field].extend(
            {
                "at": (first + timedelta(seconds=index)).isoformat(),
                "state": f"{value:.45f}",
                "unit": unit,
            }
            for index in range(1, 1750)
        )
        body[field].sort(key=lambda row: row["at"])
    return body


def raw_captures(body):
    """Yield successive real-time frames with unchanged-value report heartbeats and original state times."""
    from copy import deepcopy

    from open_energy_planner.appliances.cycles.model import instant

    first, end = instant(body["start"]), instant(body["end"])
    first = max(first, instant(body["power_states"][0]["at"]))
    captured = first
    while captured <= end:
        frame = deepcopy(body)
        frame["observer_session"] = body.get("observer_session", "synthetic-live-observer")
        frame["start"] = min(instant(body["start"]), captured - timedelta(seconds=1)).isoformat()
        frame["end"] = captured.isoformat()
        for field in ("power_states", "energy_states"):
            frame[field] = [row for row in frame[field] if instant(row["at"]) <= captured]
        frame["power_reported_at"] = captured.isoformat()
        frame["energy_reported_at"] = captured.isoformat()
        yield frame
        if captured == end:
            break
        captured = min(captured + timedelta(minutes=5), end)


def follow_raw_trace(service, body, *, restart_at=None, gap_at=None):
    """Replay live captures with optional observer loss, preserving actual service admission and storage."""
    from open_energy_planner.appliances.cycles.model import instant
    from open_energy_planner.appliances.cycles.service import ApplianceService

    for frame in raw_captures(body):
        captured = instant(frame["end"])
        if captured == restart_at:
            service = ApplianceService(service.documents, service.consumers)
        if gap_at is None or not gap_at[0] <= captured < gap_at[1]:
            service.learn_raw_history(frame, captured)
    return service


def start_observing(service, body):
    """Observe all preceding captures so a test can exercise admission of its final real frame."""
    from open_energy_planner.appliances.cycles.model import instant

    preceding = {**body, "end": (instant(body["end"]) - timedelta(minutes=5)).isoformat()}
    return follow_raw_trace(service, preceding)
