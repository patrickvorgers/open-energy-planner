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
