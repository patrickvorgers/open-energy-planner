"""Replaceable Open-Meteo temperature adapter. Numerical models never import provider payloads."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import ceil

from aiohttp import ClientSession, ClientTimeout

from open_energy_planner.core.units import BYTES_PER_KIB, HOURS_PER_DAY
from open_energy_planner.demand.calculation.model import TemperatureHour

from ..planning_policy import PLANNING_HORIZON_HOURS

WEATHER_READ_CHUNK_BYTES = 16384
MAX_WEATHER_HOURS = 9000
RECENT_WEATHER_DAYS = 7
ARCHIVE_WEATHER_LAG_DAYS = 8
WEATHER_TIMEOUT_SECONDS = 10
ARCHIVE_LOOKBACK_DAYS = 367


class OpenMeteoTemperatureSource:
    """Fetch Celsius temperatures for HA's location with bounded requests and daily historical caching."""

    def __init__(self, session, history_start=None):
        """Borrow one app-owned HTTP session. No keys or HA credentials are involved."""
        self.session = session
        self.history_start = history_start
        self.archive = {}
        self.past_key, self.past = None, ()

    async def read(self, location, now):
        """Return realized historical and forecast hours, retaining receipt as the knowledge time.

        Newly downloaded past weather was not necessarily available at an old
        decision time. Backtests must filter known_at rather than silently treat
        reanalysis as an archived historical forecast.
        """
        params = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "hourly": "temperature_2m",
            "timezone": "UTC",
            "timeformat": "unixtime",
        }
        first = now.date() - timedelta(days=ARCHIVE_LOOKBACK_DAYS)
        measured_start = self.history_start() if self.history_start else None
        if measured_start is not None:
            first = min(first, measured_start.date())
        key = location.latitude, location.longitude, now.date(), first
        if key != self.past_key:
            end = now.date() - timedelta(days=ARCHIVE_WEATHER_LAG_DAYS)
            archive_keys = tuple(
                (
                    location.latitude,
                    location.longitude,
                    max(first, datetime(year, 1, 1).date()),
                    min(end, datetime(year, 12, 31).date()),
                )
                for year in range(first.year, end.year + 1)
            )
            self.archive = {
                archive_key: hours
                for archive_key, hours in self.archive.items()
                if archive_key in archive_keys
            }
            past = []
            for archive_key in archive_keys:
                if archive_key not in self.archive:
                    self.archive[archive_key] = await self._read(
                        "https://archive-api.open-meteo.com/v1/archive",
                        {
                            **params,
                            "start_date": archive_key[2].isoformat(),
                            "end_date": archive_key[3].isoformat(),
                        },
                        now,
                    )
                past.extend(self.archive[archive_key])
            self.past = tuple(past)
            self.past_key = key
        recent = await self._read(
            "https://api.open-meteo.com/v1/forecast",
            {
                **params,
                "past_days": RECENT_WEATHER_DAYS,
                "forecast_days": max(3, ceil(PLANNING_HORIZON_HOURS / HOURS_PER_DAY) + 1),
            },
            now,
        )
        return tuple(
            sorted(
                {item.start: item for item in (*self.past, *recent)}.values(),
                key=lambda item: item.start,
            )
        )

    async def _read(self, url, params, now):
        """Normalize only supported hourly Celsius data, bounded to 2 MiB and 9000 values."""
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
                if len(raw) > 2 * BYTES_PER_KIB * BYTES_PER_KIB:
                    raise ValueError("Weather response exceeds size limit")
        import json

        body = json.loads(raw)
        if body.get("hourly_units", {}).get("temperature_2m") != "°C":
            raise ValueError("Unsupported temperature unit")
        hours = body["hourly"]
        times, values = hours["time"], hours["temperature_2m"]
        if (
            not isinstance(times, list)
            or not isinstance(values, list)
            or len(times) != len(values)
            or len(times) > MAX_WEATHER_HOURS
        ):
            raise ValueError("Invalid weather coverage")
        result = []
        for stamp, value in zip(times, values, strict=True):
            if value is None:
                continue
            if type(stamp) not in (int, float):
                raise ValueError("Invalid weather timestamp")
            result.append(TemperatureHour(datetime.fromtimestamp(stamp, UTC), value, now))
        if any(
            previous_item.start >= next_item.start for previous_item, next_item in pairwise(result)
        ):
            raise ValueError("Unordered weather timestamps")
        return tuple(result)


@asynccontextmanager
async def weather_factory(history_start=None):
    """Share a bounded app-owned session for the selected default temperature source."""
    async with ClientSession() as session:
        yield OpenMeteoTemperatureSource(session, history_start=history_start)
