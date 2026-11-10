"""Preserve Solar's existing wire names and durable history cohorts using the shared importer."""

from open_energy_planner.history.importer import (
    ALIGNMENT_POLICY,
    HISTORY_EPOCH,
    MAX_ALIGNMENT_SECONDS,
    MAX_REQUEST_DAYS,
    HistoryContinuityError,
    HistoryImport,
)
from open_energy_planner.history.importer import (
    CumulativeHistory as SolarEnergyHistory,
)
from open_energy_planner.history.importer import (
    EnergyEndpoint as SolarEnergyHour,
)
from open_energy_planner.history.importer import (
    HistoryReport as SolarHistoryReport,
)
from open_energy_planner.history.importer import (
    parse_report as parse_history_report,
)

DISCOVERY_BASIS = "solar_history_range"
CHUNK_BASIS = "solar_history_chunk"

__all__ = [
    "ALIGNMENT_POLICY",
    "HISTORY_EPOCH",
    "MAX_ALIGNMENT_SECONDS",
    "MAX_REQUEST_DAYS",
    "HistoryContinuityError",
    "SolarEnergyHistory",
    "SolarEnergyHour",
    "SolarHistoryImport",
    "SolarHistoryReport",
    "parse_report",
]


def parse_report(body):
    """Read the unchanged Solar protocol without altering retained chunk identities."""
    if (
        isinstance(body, dict)
        and body.get("basis") == CHUNK_BASIS
        and "unit" in body
        and body["unit"] != "kWh"
    ):
        raise ValueError("Unsupported solar history unit")
    return parse_history_report(body, discovery_basis=DISCOVERY_BASIS, chunk_basis=CHUNK_BASIS)


class SolarHistoryImport(HistoryImport):
    """Retain Solar-specific protocol tags while sharing the existing bounded import lifecycle."""

    def __init__(self, store):
        """Borrow the original Solar store and its serialized archive transactions."""
        super().__init__(store, discovery_basis=DISCOVERY_BASIS, chunk_basis=CHUNK_BASIS)
