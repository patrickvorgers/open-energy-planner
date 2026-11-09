"""Independent original 72-hour predictions and completed measurements for daily accuracy tests."""

from datetime import UTC, datetime, timedelta

START = datetime(2026, 10, 22, 22, tzinfo=UTC)


def forecasts(now, *, baseline=0.5, candidate=0.2):
    """Forecast 72 complete UTC hours using independently specified hourly kWh values."""
    return {
        name: {
            "issued_at": now.isoformat(),
            "model": name,
            "steps": [
                {
                    "start": (now.replace(minute=0) + timedelta(hours=index)).isoformat(),
                    "duration_h": 1,
                    "kwh": value,
                }
                for index in range(73)
            ],
        }
        for name, value in (("baseline", baseline), ("candidate", candidate))
    }


def measurements(now):
    """Supply only completed measured hours at 0.2 kWh, never future outcomes."""
    return {
        START + timedelta(hours=index): 0.2
        for index in range(int((now - START) / timedelta(hours=1)))
    }
