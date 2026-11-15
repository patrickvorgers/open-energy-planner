"""Shared synthetic appliance history inputs and bounded-upload helpers without scenario imports."""

from datetime import UTC, datetime, timedelta

from open_energy_planner.appliances.cycles import evidence, history
from open_energy_planner.appliances.cycles.model import instant

NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)
ENTITY = "sensor.washer_energy"
POWER = "sensor.washer_power"


def readings(*, cycles=2):
    """Each 75-minute cycle consumes .5 kWh, with a fifteen-minute zero-power pause.

    Three five-minute bins at 1000 W, three at zero and nine at 333 1/3 W
    total .25 + .25 = .5 kWh. Quiet brackets never enter the measured profile.
    """
    start, total, rows = NOW - timedelta(days=2), 10.0, []
    powers = [0] * 12 + [1000] * 3 + [0] * 3 + [1000 / 3] * 9 + [0] * 12
    for index, power in enumerate(powers * cycles):
        total += power / 12000
        rows.append(
            {
                "at": (start + timedelta(minutes=index * 5)).isoformat(),
                "power_w": power,
                "kwh": total,
            }
        )
    return rows


def running_readings():
    """Expose 45 minutes of the known 75-minute cycle, ending exactly at NOW."""
    from open_energy_planner.appliances.cycles.model import instant

    rows = readings(cycles=1)[:21]
    offset = NOW - (instant(rows[-1]["at"]) + timedelta(minutes=5))
    return [{**row, "at": (instant(row["at"]) + offset).isoformat()} for row in rows]


def complete_running_readings():
    """Return the full same run plus sixty quiet minutes, completing ninety minutes after NOW."""
    from open_energy_planner.appliances.cycles.model import instant

    rows = readings(cycles=1)
    offset = instant(running_readings()[0]["at"]) - instant(rows[0]["at"])
    return [{**row, "at": (instant(row["at"]) + offset).isoformat()} for row in rows]


def explicit_three_kwh(service, *, cycle_id="explicit-three"):
    """Record a distinct one-hour, 3-kWh completed energy-counter run without claiming a power sensor."""
    start = NOW - timedelta(days=1)
    points = [
        {"at": (start + timedelta(minutes=minute)).isoformat(), "kwh": minute / 20}
        for minute in (0, 15, 30, 45, 60)
    ]
    service.record_cycle(ENTITY, cycle_id, start, start + timedelta(hours=1), points)


def start_fine(service, start, end, now):
    """Use the real source-bound scan admission contract."""
    history.scan(
        service,
        ENTITY,
        POWER,
        "processing",
        start.isoformat(),
        end.isoformat(),
        now,
        resolution="5minute",
    )


def next_batch(service, rows, now, *, resolution="5minute"):
    """Upload exactly the requested bounded range, retaining full detector overlap."""
    pending = history.request(service.state["devices"][ENTITY], resolution=resolution)
    assert pending is not None
    assert instant(pending["end"]) - instant(pending["start"]) <= timedelta(days=7)
    selected = [
        row
        for row in rows
        if instant(pending["read_start"]) <= instant(row["at"]) < instant(pending["read_end"])
    ]
    history.batch(
        service, ENTITY, POWER, selected, False, **pending, now=now, resolution=resolution
    )
    return pending


def trace_refinement(monkeypatch):
    """Count actual model reconstruction while allowing the original numerical implementation to run."""
    calls = []
    original = evidence.refine

    def tracked(settings, records):
        """Record complete refinement invocations, including empty archive sets."""
        calls.append(len(records))
        return original(settings, records)

    monkeypatch.setattr(evidence, "refine", tracked)
    return calls


def hourly_rows(start, powers):
    """Pair hourly mean W with cumulative kWh at interval end, starting from a negative origin."""
    total, rows = -50.0, []
    for index, power in enumerate(powers):
        total += power / 1000
        rows.append(
            {"at": (start + timedelta(hours=index)).isoformat(), "power_w": power, "kwh": total}
        )
    return rows


def begin(service, start, end):
    """Initialize the same source-bound range used by the Recorder transport."""
    history.scan(service, ENTITY, POWER, "processing", start.isoformat(), end.isoformat(), NOW)


def upload(service, rows):
    """Send only the currently requested core with the detector's independent thirteen-hour overlap."""
    pending = service.document()["devices"][0]["history_request"]
    first, last = instant(pending["start"]), instant(pending["end"])
    bounded = [
        row
        for row in rows
        if first - timedelta(hours=13) <= instant(row["at"]) < last + timedelta(hours=1)
    ]
    history.batch(service, ENTITY, POWER, bounded, False, **pending, now=NOW)
    return pending
