"""Bounded Open-Meteo solar-weather adapter with explicit interval and knowledge semantics."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from math import ceil, isfinite

from aiohttp import ClientError, ClientSession, ClientTimeout

from open_energy_planner.core.units import BYTES_PER_KIB, HOURS_PER_DAY
from open_energy_planner.solar.calculation.model import SolarWeatherHour

from ..planning_policy import PLANNING_HORIZON_HOURS

WEATHER_READ_CHUNK_BYTES = 16384
MAX_WEATHER_RESPONSE_BYTES = 4 * BYTES_PER_KIB * BYTES_PER_KIB
MAX_WEATHER_HOURS = 9000
MIN_FORECAST_CALENDAR_DAYS = 4
RECENT_WEATHER_DAYS = 7
WEATHER_TIMEOUT_SECONDS = 15
ARCHIVE_LOOKBACK_DAYS = 367
ARCHIVE_WEATHER_LAG_DAYS = 8

FIELDS = (
    "shortwave_radiation",
    "direct_normal_irradiance",
    "diffuse_radiation",
    "temperature_2m",
    "wind_speed_10m",
)


class OpenMeteoSolarSource:
    """Fetch common weather once per site, not once per inverter, with daily historical caching."""

    def __init__(self, session):
        """Borrow an app-owned session without API keys or HA credentials."""
        self.session = session
        self.past_key, self.past = None, ()

    async def read(self, location, now):
        """Return historical and future hours. Newly received history is not an old issued forecast."""
        params = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "hourly": ",".join(FIELDS),
            "timezone": "UTC",
            "timeformat": "unixtime",
            "wind_speed_unit": "ms",
        }
        recent = await self._read(
            "https://api.open-meteo.com/v1/forecast",
            {
                **params,
                "forecast_days": max(
                    MIN_FORECAST_CALENDAR_DAYS, ceil(PLANNING_HORIZON_HOURS / HOURS_PER_DAY) + 1
                ),
                "past_days": RECENT_WEATHER_DAYS,
            },
            now,
        )
        # Future weather remains usable even if an optional archive request fails.
        key = location.latitude, location.longitude, now.date()
        if key != self.past_key:
            try:
                past = await self._read(
                    "https://archive-api.open-meteo.com/v1/archive",
                    {
                        **params,
                        "start_date": (
                            now.date() - timedelta(days=ARCHIVE_LOOKBACK_DAYS)
                        ).isoformat(),
                        "end_date": (
                            now.date() - timedelta(days=ARCHIVE_WEATHER_LAG_DAYS)
                        ).isoformat(),
                    },
                    now,
                )
            except (ClientError, OSError, ValueError, TimeoutError):
                past = ()
            else:
                self.past_key = key
            self.past = past
        return tuple(
            sorted(
                {weather.start: weather for weather in (*self.past, *recent)}.values(),
                key=lambda weather: weather.start,
            )
        )

    async def _read(self, url, params, now):
        """Normalize end-labelled hours and finite negative provider irradiance.

        Irradiance has a physical zero floor. Missing hours remain absent and
        malformed or excessive quantities still fail the solar model validation.
        """
        async with self.session.get(
            url,
            params=params,
            timeout=ClientTimeout(total=WEATHER_TIMEOUT_SECONDS),
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            async for part in response.content.iter_chunked(WEATHER_READ_CHUNK_BYTES):
                raw.extend(part)
                if len(raw) > MAX_WEATHER_RESPONSE_BYTES:
                    raise ValueError("Solar weather response too large")
        body = json.loads(raw)
        if not isinstance(body, dict) or "hourly" not in body:
            raise ValueError("Invalid solar weather response")
        units, hours = body.get("hourly_units"), body.get("hourly")
        if not isinstance(units, dict) or not isinstance(hours, dict) or "time" not in hours:
            raise ValueError("Invalid solar weather response")
        expected = ("W/m²", "W/m²", "W/m²", "°C", "m/s")
        if any(units.get(key) != unit for key, unit in zip(FIELDS, expected, strict=True)):
            raise ValueError("Unsupported solar weather units")
        times = hours.get("time")
        if (
            not isinstance(times, list)
            or len(times) > MAX_WEATHER_HOURS
            or any(
                not isinstance(hours.get(key), list) or len(hours[key]) != len(times)
                for key in FIELDS
            )
        ):
            raise ValueError("Invalid solar weather series")
        result = []
        previous = None
        for index, stamp in enumerate(times):
            if type(stamp) not in (int, float) or (previous is not None and stamp <= previous):
                raise ValueError("Invalid solar weather order")
            previous = stamp
            try:
                start = datetime.fromtimestamp(stamp, UTC) - timedelta(hours=1)
            except (OverflowError, OSError, ValueError):
                raise ValueError("Invalid solar weather timestamp") from None
            values = tuple(hours[key][index] for key in FIELDS)
            if None in values:
                continue
            irradiance = tuple(
                0.0
                if (type(value) is int or type(value) is float and isfinite(value)) and value < 0
                else value
                for value in values[:3]
            )
            # Radiation is the preceding-hour mean. Temperature/wind are sampled
            # at that hour's end and used as an explicit approximation for it.
            result.append(SolarWeatherHour(start, *irradiance, *values[3:], now))
        return tuple(result)


@asynccontextmanager
async def solar_weather_factory():
    """Create one solar-weather session for the local app lifecycle."""
    async with ClientSession() as session:
        yield OpenMeteoSolarSource(session)
