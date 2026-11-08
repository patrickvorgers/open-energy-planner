"""Bounded Open-Meteo solar-weather adapter with explicit interval and knowledge semantics."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from math import ceil, isfinite

from aiohttp import ClientError, ClientSession, ClientTimeout

from open_energy_planner.core.units import BYTES_PER_KIB, HOURS_PER_DAY
from open_energy_planner.prices.model import aware
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog
from open_energy_planner.solar.calculation.model import SolarWeatherHour, version
from open_energy_planner.solar.storage.weather import (
    WEATHER_NORMALIZATION_REVISION,
    HistoricalWeatherCache,
)

from ..planning_policy import PLANNING_HORIZON_HOURS

WEATHER_READ_CHUNK_BYTES = 16384
MAX_WEATHER_RESPONSE_BYTES = 4 * BYTES_PER_KIB * BYTES_PER_KIB
MAX_WEATHER_HOURS = 9000
MIN_FORECAST_CALENDAR_DAYS = 4
RECENT_WEATHER_DAYS = 7
WEATHER_TIMEOUT_SECONDS = 15
ARCHIVE_LOOKBACK_DAYS = 367
ARCHIVE_WEATHER_LAG_DAYS = 8
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

FIELDS = (
    "shortwave_radiation",
    "direct_normal_irradiance",
    "diffuse_radiation",
    "temperature_2m",
    "wind_speed_10m",
)

_LOGGER = logging.getLogger(__name__)


class OpenMeteoSolarSource:
    """Fetch common weather once per site, not once per inverter, with daily historical caching."""

    def __init__(self, session, store=None, history_start=None):
        """Borrow transport, archive and a getter for the oldest actually loaded measurement hour."""
        self.session = session
        self.cache = HistoricalWeatherCache(store) if store is not None else None
        self.history_start = history_start
        self.past_key, self.past = None, ()
        self.archive_status = BackgroundLog(_LOGGER, "Historical solar weather refresh")

    async def read(self, location, now):
        """Return historical and future hours. Newly received history is not an old issued forecast."""
        first = now.date() - timedelta(days=ARCHIVE_LOOKBACK_DAYS)
        oldest = self.history_start() if self.history_start is not None else None
        if oldest is not None:
            oldest = aware(oldest)
            first = min(first, oldest.date() - timedelta(days=1))
        params = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "hourly": ",".join(FIELDS),
            "timezone": "UTC",
            "timeformat": "unixtime",
            "wind_speed_unit": "ms",
        }
        recent = await self._read(
            FORECAST_URL,
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
        key = version(
            {
                "url": ARCHIVE_URL,
                "params": params,
                "day": now.date().isoformat(),
                "history_start": oldest.isoformat() if oldest is not None else None,
                "start_date": first.isoformat(),
                "normalization": WEATHER_NORMALIZATION_REVISION,
            }
        )
        if key != self.past_key:
            try:
                past, complete = await self._historical(params, now, first)
            except (ClientError, OSError, ValueError, TimeoutError) as error:
                past = ()
                self.archive_status.exception(
                    "Current weather is retained. Historical model training has less evidence. Retrying on the next weather refresh.",
                    expected=True,
                    reason="invalid historical weather" if isinstance(error, ValueError) else None,
                )
            else:
                if complete:
                    self.past_key = key
                if not complete:
                    self.archive_status.unavailable(
                        "Current weather and complete cached history are retained. Missing historical weather is retried on the next weather refresh.",
                        reason="historical weather coverage is incomplete or unavailable",
                    )
                elif past:
                    self.archive_status.success(
                        "Historical weather is available for solar model training."
                    )
                else:
                    self.archive_status.unavailable(
                        "Current weather is retained. Historical model training has less evidence. The archive is checked again after the UTC day or site changes.",
                        reason="no usable historical weather hours were returned",
                    )
            self.past = past
        return tuple(
            sorted(
                {weather.start: weather for weather in (*self.past, *recent)}.values(),
                key=lambda weather: weather.start,
            )
        )

    async def _historical(self, params, now, first):
        """Reuse complete monthly prefixes and fetch only uncached provider calendar dates.

        Cached receipts remain their original actual retrieval times. A failed
        or partial suffix never advances durable coverage. Other complete months
        and the current forecast remain usable. Database work runs outside the
        event loop and uses the app-owned active archive only.
        """
        last = now.date() - timedelta(days=ARCHIVE_WEATHER_LAG_DAYS)
        if self.cache is None and ((last - first).days + 1) * 24 <= MAX_WEATHER_HOURS:
            rows = await self._read(
                ARCHIVE_URL,
                {**params, "start_date": first.isoformat(), "end_date": last.isoformat()},
                now,
            )
            return rows, True
        month, complete, result = first.replace(day=1), True, []
        archive_available = True
        while month <= last:
            following_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            end = min(last, following_month - timedelta(days=1))
            rows = ()
            try:
                if self.cache is not None:
                    rows, covered = await asyncio.to_thread(
                        self.cache.read, ARCHIVE_URL, params, month
                    )
                else:
                    covered = month - timedelta(days=1)
                missing = covered + timedelta(days=1)
                if missing <= end and archive_available:
                    try:
                        suffix = await self._read(
                            ARCHIVE_URL,
                            {
                                **params,
                                "start_date": missing.isoformat(),
                                "end_date": end.isoformat(),
                            },
                            now,
                        )
                    except (ClientError, OSError, ValueError, TimeoutError):
                        # One provider outage must not multiply its timeout by years of history.
                        archive_available = False
                        raise
                    combined = (*rows, *suffix)
                    if self.cache is not None:
                        await asyncio.to_thread(
                            self.cache.write, ARCHIVE_URL, params, month, end, combined
                        )
                    else:
                        HistoricalWeatherCache._complete(combined, month, end)
                    rows = combined
                elif missing <= end:
                    complete = False
            except (ClientError, OSError, ValueError, TimeoutError):
                complete = False
            result.extend(rows)
            month = following_month
        start = datetime.combine(first, datetime.min.time(), UTC) - timedelta(hours=1)
        stop = datetime.combine(last + timedelta(days=1), datetime.min.time(), UTC) - timedelta(
            hours=1
        )
        return tuple(row for row in result if start <= row.start < stop), complete

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
async def solar_weather_factory(store=None, history_start=None):
    """Create one transport with optional durable historical caching in the active solar archive."""
    async with ClientSession() as session:
        yield OpenMeteoSolarSource(session, store, history_start)
