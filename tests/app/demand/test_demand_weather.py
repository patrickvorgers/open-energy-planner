"""Bounded replaceable weather-adapter fixtures. Never contact the external provider in tests."""

import asyncio
import json

import pytest

from open_energy_planner.demand.weather import OpenMeteoTemperatureSource
from open_energy_planner.installation.location import SiteLocation
from tests.app.demand.test_demand import START


class Response:
    """Provide the narrow async HTTP response contract used by the weather adapter."""

    def __init__(self, body):
        """Bind bytes and expose the streaming body interface without a network."""
        self.raw = body
        self.content = self

    async def __aenter__(self):
        """Return this successful isolated response."""
        return self

    async def __aexit__(self, *_args):
        """Close the fixture context without suppressing validation failures."""

    def raise_for_status(self):
        """Represent a successful HTTP status."""

    async def iter_chunked(self, size):
        """Yield bounded stream parts so response-size protection is exercised."""
        for index in range(0, len(self.raw), size):
            yield self.raw[index : index + size]


class Session:
    """Capture exact provider parameters while returning independent temperature fixtures."""

    def __init__(self, *, unit="°C", oversized=False):
        """Select valid/malformed units or an oversized response."""
        self.calls, self.unit, self.oversized = [], unit, oversized

    def get(self, url, **kwargs):
        """Return two hourly values with an explicit missing temperature, never zero substitution."""
        self.calls.append((url, kwargs))
        raw = json.dumps(
            {
                "hourly_units": {"temperature_2m": self.unit},
                "hourly": {
                    "time": [START.timestamp(), START.timestamp() + 3600],
                    "temperature_2m": [5, None],
                },
            }
        ).encode()
        return Response(b"x" * (2 * 1024 * 1024 + 1) if self.oversized else raw)


def test_weather_adapter_uses_ha_location_units_receipt_time_and_daily_archive_cache():
    """Use Celsius at HA coordinates, keep the missing hour absent and fetch historical temperatures only once per day."""

    async def scenario():
        """Exercise real adapter normalization with bounded fake HTTP, without external traffic."""
        session = Session()
        source = OpenMeteoTemperatureSource(session)
        location = SiteLocation(52, 6, "Europe/Amsterdam")
        result = await source.read(location, START)
        assert len(result) == 1
        assert result[0].celsius == 5
        assert result[0].known_at == START
        await source.read(location, START)
        assert len(session.calls) == 3
        assert session.calls[0][1]["params"]["latitude"] == 52
        assert session.calls[0][1]["params"]["longitude"] == 6
        assert session.calls[1][1]["params"]["forecast_days"] == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("options", [{"unit": "°F"}, {"oversized": True}])
def test_weather_adapter_rejects_wrong_units_and_unbounded_responses(options):
    """Fahrenheit or an oversized provider response cannot silently become usable Celsius model input."""

    async def scenario():
        """Reject malformed provider evidence before it reaches the forecaster."""
        with pytest.raises(ValueError):
            await OpenMeteoTemperatureSource(Session(**options)).read(
                SiteLocation(52, 6, "Europe/Amsterdam"), START
            )

    asyncio.run(scenario())
