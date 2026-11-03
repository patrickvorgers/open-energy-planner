"""Strict compact hourly-energy transport. No provider dictionaries enter calculation code."""

from datetime import UTC, datetime

from open_energy_planner.history.model import EnergyHistory, EnergyHour

MAX_HISTORY_ROWS = 9000


def parse_history(body):
    """Parse bounded hourly cumulative energy history.

    Args:
        body: Compact report whose row starts are Unix seconds on whole UTC
            hours. Statistics sums and physical meter readings use cumulative kWh.

    Returns:
        EnergyHistory: Ordered, normalized history with optional component meters.

    Raises:
        ValueError: Invalid fields, units, values, row count or timestamps,
            including platform-dependent timestamp conversion failures.
    """
    if not isinstance(body, dict) or set(body) - {"component_meters"} != {
        "request_version",
        "entity_id",
        "unit",
        "hours",
    }:
        raise ValueError("Invalid history fields")
    if body["unit"] != "kWh" or not isinstance(body["request_version"], str):
        raise ValueError("Unsupported history unit/request")
    rows = body["hours"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_HISTORY_ROWS:
        raise ValueError("Invalid history size")
    hours = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 3 or type(row[0]) not in (int, float):
            raise ValueError("Invalid history row")
        try:
            instant = datetime.fromtimestamp(row[0], UTC)
        except (OverflowError, OSError, ValueError):
            raise ValueError("Invalid history timestamp") from None
        hours.append(EnergyHour(instant, row[1], row[2]))
    return EnergyHistory(body["entity_id"], tuple(hours), body.get("component_meters", ()))
