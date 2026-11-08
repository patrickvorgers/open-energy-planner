"""Shared Recorder transfer fixtures with explicit runtime and database ownership."""

from datetime import UTC, datetime, timedelta

from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.storage.history import parse_report
from tests.app.solar.support import plant
from tests.database.support.archive import make_store

HISTORY_NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)


HISTORY_IDENTITY = {"metadata_id": 7, "source": "recorder", "unit": "kWh"}


def runtime_case(tmp_path, *, count=1):
    """Build synthetic source identities with an actual active-database solar namespace."""
    store = make_store(tmp_path, SolarStore)
    store.save(plant(count=count))
    return SolarRuntime(store)


def discover(runtime, now=HISTORY_NOW, *, first="2023-01-01T00:00:00+00:00"):
    """Install one source's explicit oldest monthly bucket without transporting its hourly years."""
    pending = runtime.request(now)
    body = {
        "basis": "solar_history_range",
        "request_version": pending["version"],
        "entity_id": pending["entity_id"],
        "statistics_identity": HISTORY_IDENTITY,
        "available_start": first,
    }
    report = parse_report(body)
    runtime.receive(pending["version"], None, now, report=report)
    return pending


def chunk_report(pending, *, gaps=(), empty=False, identity=None, changed_overlap=False):
    """Generate independent one-kWh hourly increments, preserving requested gaps and the closing overlap."""
    start, end = (datetime.fromisoformat(pending[key]) for key in ("start", "end"))
    rows = []
    instant = start
    while instant < end and not empty:
        if instant not in gaps:
            total = instant.timestamp() / 3600
            if changed_overlap and instant == start:
                total += 5
            rows.append([instant.timestamp(), total, total])
        instant += timedelta(hours=1)
    return parse_report(
        {
            "request_version": pending["version"],
            "basis": "solar_history_chunk",
            "entity_id": pending["entity_id"],
            "statistics_identity": identity or HISTORY_IDENTITY,
            "unit": "kWh",
            "hours": rows,
        }
    )


def finish(runtime, now=HISTORY_NOW, *, gaps=(), empty_years=()):
    """Follow explicit annual requests to completion without interpreting empty years as the end."""
    requests = []
    while pending := runtime.request(now):
        assert pending["basis"] == "solar_history_chunk"
        requests.append(pending)
        assert len(requests) <= 5
        report = chunk_report(pending, gaps=gaps, empty=pending["import_year"] in empty_years)
        runtime.receive(pending["version"], report.history, now, report=report)
    return requests
