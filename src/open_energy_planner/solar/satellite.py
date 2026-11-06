"""Read observed satellite GHI independently of planning and model-training weather."""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiohttp import ClientSession, ClientTimeout

from .calculation.model import MAX_IRRADIANCE_W_M2, number
from .weather import (
    MAX_WEATHER_RESPONSE_BYTES,
    WEATHER_READ_CHUNK_BYTES,
    WEATHER_TIMEOUT_SECONDS,
)

SATELLITE_MODEL = "dwd_sis_europe_africa_v4"
SATELLITE_LABEL = "Open-Meteo: DWD / EUMETSAT MTG"
MAX_SATELLITE_HOURS = 72
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class RadiationHour:
    """One complete UTC hour's global horizontal irradiance in W/m²."""

    start: datetime
    ghi: float


class OpenMeteoSatelliteSource:
    """Pin one named satellite dataset instead of silently falling back to model weather."""

    def __init__(self, session):
        """Borrow an app-owned HTTP session without credentials or redirects."""
        self.session = session

    async def read(self, location, start, end, now):
        """Read a bounded day including its end-labelled UTC edge hour.

        Nulls stay absent. Future or incomplete hours never establish observed
        coverage. The provider's hourly means refer to the preceding hour.
        """
        if not timedelta(0) < end - start <= timedelta(hours=25):
            raise ValueError("Expected one bounded satellite day")
        params = {
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "hourly": "shortwave_radiation",
            "models": SATELLITE_MODEL,
            "timezone": "UTC",
            "timeformat": "unixtime",
            "start_date": start.date().isoformat(),
            "end_date": end.date().isoformat(),
        }
        async with self.session.get(
            "https://satellite-api.open-meteo.com/v1/archive",
            params=params,
            timeout=ClientTimeout(total=WEATHER_TIMEOUT_SECONDS),
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            async for part in response.content.iter_chunked(WEATHER_READ_CHUNK_BYTES):
                raw.extend(part)
                if len(raw) > MAX_WEATHER_RESPONSE_BYTES:
                    raise ValueError("Satellite response too large")
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get("hourly_units"), dict):
            raise TypeError("Invalid satellite response")
        if body["hourly_units"].get("shortwave_radiation") != "W/m²":
            raise ValueError("Unsupported satellite units")
        hours = body.get("hourly")
        if not isinstance(hours, dict):
            raise TypeError("Invalid satellite hours")
        times, values = hours.get("time"), hours.get("shortwave_radiation")
        if (
            not isinstance(times, list)
            or not isinstance(values, list)
            or len(times) != len(values)
            or len(times) > MAX_SATELLITE_HOURS
        ):
            raise ValueError("Invalid satellite series")
        result, previous = [], None
        for stamp, ghi in zip(times, values, strict=True):
            if type(stamp) not in (int, float) or previous is not None and stamp <= previous:
                raise ValueError("Invalid satellite order")
            try:
                instant = datetime.fromtimestamp(stamp, UTC) - HOUR
            except (OverflowError, OSError, ValueError):
                raise ValueError("Invalid satellite timestamp") from None
            if instant.minute or instant.second or instant.microsecond:
                raise ValueError("Satellite requires UTC hours")
            previous = stamp
            if ghi is None:
                continue
            number(ghi, 0, MAX_IRRADIANCE_W_M2)
            if start <= instant and instant + HOUR <= min(end, now):
                result.append(RadiationHour(instant, ghi))
        return tuple(result)


@asynccontextmanager
async def satellite_factory():
    """Own the satellite connection for one application lifecycle."""
    async with ClientSession() as session:
        yield OpenMeteoSatelliteSource(session)
