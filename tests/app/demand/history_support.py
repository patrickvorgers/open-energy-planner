"""Independent synthetic Recorder reports for the bounded demand-history protocol."""

from datetime import datetime

from open_energy_planner.history.importer import parse_report


def history_report(pending, history):
    """Build discovery or bounded cumulative evidence from an independently specified channel."""
    body = {
        "request_version": pending["version"],
        "basis": pending["basis"],
        "entity_id": pending["entity_id"],
        "statistics_identity": {"metadata_id": 17, "source": "recorder", "unit": "kWh"},
    }
    if pending["basis"] == "energy_history_range":
        return {**body, "available_start": history.hours[0].start.isoformat()}
    start, end = (datetime.fromisoformat(pending[key]) for key in ("start", "end"))
    return {
        **body,
        "unit": "kWh",
        "hours": [
            [hour.start.timestamp(), hour.total_kwh, getattr(hour, "meter_kwh", None)]
            for hour in history.hours
            if start <= hour.start < end
        ],
    }


def receive_source(runtime, history, now):
    """Complete one selected source without advancing another meter's cohort."""
    pending = runtime.request(now)
    entity = history.entity_id
    while pending is not None and pending["entity_id"] == entity:
        runtime.receive(pending["version"], parse_report(history_report(pending, history)), now)
        pending = runtime.request(now)
