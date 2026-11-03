"""Provide independently specified historical forecast values without opening a database."""

from datetime import UTC, datetime, timedelta

START = datetime(2026, 10, 26, 12, tzinfo=UTC)


def prediction():
    """Twelve quarter predictions of .25 kWh give three independent 1-kWh hourly predictions."""
    return {
        "model": "weekday-hour-v2",
        "version": "original",
        "issued_at": (START - timedelta(minutes=5)).isoformat(),
        "steps": [
            {
                "start": (START + timedelta(minutes=15 * interval_index)).isoformat(),
                "duration_h": 0.25,
                "kwh": 0.25,
            }
            for interval_index in range(12)
        ],
    }
