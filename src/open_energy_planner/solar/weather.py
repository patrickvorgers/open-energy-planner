"""Bounded Open-Meteo solar-weather adapter with explicit interval and knowledge semantics."""

import asyncio
import json
import logging
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from math import ceil, isfinite

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout

from open_energy_planner.core.units import BYTES_PER_KIB, HOURS_PER_DAY
from open_energy_planner.prices.model import aware
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog
from open_energy_planner.solar.calculation.model import SolarWeatherHour, version
from open_energy_planner.solar.forecast_weather import (
    FORECAST_RUN_RESPONSE_HOURS,
    FORECAST_WEATHER_MODEL,
    FORECAST_WEATHER_POLICY,
    MAX_LIVE_RUN_AGE_HOURS,
    RUN_CYCLE_HOURS,
    SINGLE_RUNS_URL,
    ForecastWeatherRun,
)
from open_energy_planner.solar.storage.forecast_weather import ForecastWeatherCache
from open_energy_planner.solar.storage.weather import (
    NORMALIZED_WEATHER_UNITS,
    WEATHER_NORMALIZATION_REVISION,
    HistoricalWeatherCache,
)
from open_energy_planner.storage.writes import complete_write

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
LATEST_RUN_METADATA_URL = "https://api.open-meteo.com/data/ecmwf_ifs/static/meta.json"
MAX_METADATA_RESPONSE_BYTES = 16 * BYTES_PER_KIB
HISTORICAL_RUN_BATCH_SIZE = 4
HISTORICAL_RUN_RETRY_MINUTES = 15
HISTORICAL_RUN_COMPLETE_DELAY_HOURS = 72
IFS_HISTORY_FIRST_DATE = datetime(2024, 3, 14, tzinfo=UTC).date()
HISTORICAL_RUN_INITIALIZATION_HOUR = 12

FIELDS = (
    "shortwave_radiation",
    "direct_normal_irradiance",
    "diffuse_radiation",
    "temperature_2m",
    "wind_speed_10m",
)

_LOGGER = logging.getLogger(__name__)


def forecast_weather_query(location, run_init):
    """Bind one public original run to fixed normalized fields without a simulation-origin cache key."""
    initialized = aware(run_init).astimezone(UTC)
    if (
        initialized.hour % RUN_CYCLE_HOURS
        or initialized.minute
        or initialized.second
        or initialized.microsecond
    ):
        raise ValueError("Invalid frozen solar forecast initialization")
    return {
        "url": SINGLE_RUNS_URL,
        "params": {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "hourly": ",".join(FIELDS),
            "timezone": "UTC",
            "timeformat": "unixtime",
            "wind_speed_unit": "ms",
            "models": FORECAST_WEATHER_MODEL,
            "run": initialized.strftime("%Y-%m-%dT%H:%M"),
            "forecast_hours": FORECAST_RUN_RESPONSE_HOURS,
        },
        "model": FORECAST_WEATHER_MODEL,
        "initialized_at": initialized.isoformat(),
        "location_version": version(asdict(location)),
        "normalization": WEATHER_NORMALIZATION_REVISION,
        "units": NORMALIZED_WEATHER_UNITS,
        "policy": FORECAST_WEATHER_POLICY,
    }


class OpenMeteoSolarSource:
    """Fetch current and historical original forecast runs once per site, with durable frozen caching."""

    def __init__(self, session, store=None, history_start=None):
        """Borrow transport, archive and a getter for the oldest actually loaded measurement hour."""
        self.session = session
        self.cache = HistoricalWeatherCache(store) if store is not None else None
        self.history_start = history_start
        self.past_key, self.past = None, ()
        self.archive_status = BackgroundLog(_LOGGER, "Historical solar weather refresh")
        self.current_run, self.service = None, {}
        self._run_site, self._runs = None, {}
        self._history_cursor, self._history_latest = None, None
        self._history_updates = deque()
        self.history_retry_after = None
        self._missing_run_retries, self.history_issues = {}, {}

    def _select_site(self, location):
        """Keep in-memory accepted runs and importer cursors isolated across installation locations."""
        site = version(asdict(location))
        if site != self._run_site:
            self._run_site, self._runs = site, {}
            self.current_run, self.service = None, {}
            self._history_cursor, self._history_latest = None, None
            self._history_updates.clear()
            self.history_retry_after = None
            self._missing_run_retries, self.history_issues = {}, {}
        return site

    @property
    def forecast_runs(self):
        """Return stable complete original runs with canonical simulation origins and actual receipt times."""
        return tuple(self._runs[initialized] for initialized in sorted(self._runs))

    async def read(self, location, now):
        """Select the newest actually published fixed IFS run and project its current 48-hour weather window."""
        now = aware(now).astimezone(UTC)
        site = self._select_site(location)
        async with self.session.get(
            LATEST_RUN_METADATA_URL,
            timeout=ClientTimeout(total=WEATHER_TIMEOUT_SECONDS),
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            async for part in response.content.iter_chunked(WEATHER_READ_CHUNK_BYTES):
                raw.extend(part)
                if len(raw) > MAX_METADATA_RESPONSE_BYTES:
                    raise ValueError("Solar forecast metadata response too large")
        metadata = json.loads(raw)
        if not isinstance(metadata, dict):
            raise TypeError("Invalid solar forecast metadata")
        stamps = tuple(
            metadata.get(field)
            for field in ("last_run_initialisation_time", "last_run_availability_time")
        )
        if any(type(stamp) not in (int, float) or not isfinite(stamp) for stamp in stamps):
            raise ValueError("Invalid solar forecast metadata")
        try:
            initialized, available = (datetime.fromtimestamp(stamp, UTC) for stamp in stamps)
        except (OverflowError, OSError, ValueError):
            raise ValueError("Invalid solar forecast metadata") from None
        if not initialized <= available <= now or now - initialized > timedelta(
            hours=MAX_LIVE_RUN_AGE_HOURS
        ):
            raise ValueError("Solar forecast metadata is not current and available")
        origin = now.replace(minute=0, second=0, microsecond=0)
        run = await self.read_run(location, initialized, origin, now)
        if site != self._run_site:
            raise ValueError("Solar forecast location changed")
        self.current_run = run
        self.service = {
            "model": run.model,
            "selection": "latest_available",
            "initialized_at": initialized.isoformat(),
            "received_at": run.received_at.isoformat(),
            "available_at": available.isoformat(),
            "policy": run.policy_version,
        }
        if len(run.hours) == FORECAST_RUN_RESPONSE_HOURS - 1:
            self._runs[initialized] = replace(run, forecast_origin=None)
        return run.hours

    async def import_runs(self, location, now):
        """Load at most four daily original runs per batch, independently of current weather.

        Complete active-database caches are reused once per source lifetime.
        Network failures stop the batch and back off for fifteen minutes. A
        known missing or unusable run retains its own retry and does not block
        older runs. Original corrupt cache evidence is never replaced.
        Discovery of older measurements extends the descending cursor without a
        year cap. This import never downloads observed/reanalysis weather.
        """
        now = aware(now).astimezone(UTC)
        site = self._select_site(location)
        oldest = self.history_start() if self.history_start is not None else None
        if oldest is None or self.history_retry_after and now < self.history_retry_after:
            return self.forecast_runs
        first = max(aware(oldest).astimezone(UTC).date(), IFS_HISTORY_FIRST_DATE)
        latest = (now - timedelta(hours=HISTORICAL_RUN_COMPLETE_DELAY_HOURS)).replace(
            hour=HISTORICAL_RUN_INITIALIZATION_HOUR, minute=0, second=0, microsecond=0
        )
        if latest > now - timedelta(hours=HISTORICAL_RUN_COMPLETE_DELAY_HOURS):
            latest -= timedelta(days=1)
        if self._history_latest is None:
            self._history_cursor = latest
        else:
            added = latest
            while added > self._history_latest:
                self._history_updates.append(added)
                added -= timedelta(days=1)
        self._history_latest = latest
        for _ in range(HISTORICAL_RUN_BATCH_SIZE):
            retrying = next(
                (
                    initialized
                    for initialized, retry in sorted(
                        self._missing_run_retries.items(), reverse=True
                    )
                    if retry <= now and initialized.date() >= first
                ),
                None,
            )
            updating = retrying is None and bool(self._history_updates)
            initialized = retrying or (
                self._history_updates[0] if updating else self._history_cursor
            )
            if initialized is None or initialized.date() < first:
                break
            try:
                if initialized not in self._runs:
                    run = await self.read_run(location, initialized, None, now)
                    if len(run.hours) != FORECAST_RUN_RESPONSE_HOURS - 1:
                        raise ValueError("Historical original forecast run is incomplete")
                    if site != self._run_site:
                        return self.forecast_runs
                    self._runs[initialized] = run
                self._missing_run_retries.pop(initialized, None)
                self.history_issues.pop(initialized, None)
            except (ClientResponseError, ValueError) as error:
                if isinstance(error, ClientResponseError) and error.status != 404:
                    self.history_retry_after = now + timedelta(minutes=HISTORICAL_RUN_RETRY_MINUTES)
                    raise
                self._missing_run_retries[initialized] = now + timedelta(
                    minutes=HISTORICAL_RUN_RETRY_MINUTES
                )
                self.history_issues[initialized] = (
                    "forecast_run_missing"
                    if isinstance(error, ClientResponseError)
                    else "forecast_run_unusable"
                )
            except (ClientError, OSError, TimeoutError):
                self.history_retry_after = now + timedelta(minutes=HISTORICAL_RUN_RETRY_MINUTES)
                raise
            if retrying is not None:
                continue
            if updating:
                self._history_updates.popleft()
            else:
                self._history_cursor = initialized - timedelta(days=1)
        self.history_retry_after = None
        return self.forecast_runs

    async def read_run(self, location, run_init, origin, now):
        """Read a complete fixed original run, reusing original cached receipts for every requested origin.

        No reanalysis or live best-match fallback is admitted. The provider does
        not supply historical publication metadata, so actual availability stays
        unknown. A default 24-hour origin is simulation policy only. Missing or
        corrupt retained evidence cannot be replaced by a new provider response.
        """
        query = forecast_weather_query(location, run_init)
        initialized = datetime.fromisoformat(query["initialized_at"])
        cache = ForecastWeatherCache(self.cache.store) if self.cache is not None else None
        if cache is not None:
            saved = await asyncio.to_thread(cache.read, query, origin)
            if saved is not None:
                return saved
        rows = await self._read(query["url"], query["params"], aware(now).astimezone(UTC))
        end = initialized + timedelta(hours=FORECAST_RUN_RESPONSE_HOURS - 1)
        rows = tuple(row for row in rows if initialized <= row.start < end)
        run = ForecastWeatherRun(
            initialized_at=initialized,
            forecast_origin=origin,
            received_at=aware(now).astimezone(UTC),
            location_version=query["location_version"],
            hours=rows,
        )
        if cache is not None and len(rows) == FORECAST_RUN_RESPONSE_HOURS - 1:
            accepted = [run]

            def save():
                """Finish the durable pointer transaction before cancellation can release app ownership."""
                accepted[0] = cache.write(query, run)

            cancellation = await complete_write(save)
            if cancellation:
                raise cancellation
            return accepted[0]
        return run

    async def read_observed(self, location, now):
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
