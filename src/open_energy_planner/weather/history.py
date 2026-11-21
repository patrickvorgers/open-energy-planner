"""Replaceable Open-Meteo temperature adapter. Numerical models never import provider payloads."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from itertools import groupby, pairwise

from aiohttp import ClientSession, ClientTimeout

from open_energy_planner.core.units import BYTES_PER_KIB, HOURS_PER_DAY
from open_energy_planner.storage.writes import complete_write

from .model import TemperatureHour, aware, version

WEATHER_READ_CHUNK_BYTES = 16384
MAX_WEATHER_HOURS = 9000
RECENT_WEATHER_DAYS = 7
ARCHIVE_WEATHER_LAG_DAYS = 8
WEATHER_TIMEOUT_SECONDS = 10
ARCHIVE_LOOKBACK_DAYS = 367
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
RECENT_URL = "https://api.open-meteo.com/v1/forecast"
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


class TemperatureDays:
    """Store one mutable UTC day per provider and location in the weather namespace."""

    def __init__(self, documents, url, params):
        """Bind the cache to the provider and full normalized request parameters."""
        self.documents = documents
        self.prefix = "temperature-day-" + version([url, params]) + "-"

    def read(self, first, last):
        """Read a year's day keys in one query with each hour's original knowledge time."""
        keys = {
            self.prefix + day.isoformat() + ".json": day
            for day in (first + offset * DAY for offset in range((last - first).days + 1))
        }
        return {
            keys[name]: self._restore_day(keys[name], body)
            for name, body in self.documents.read_many(keys).items()
        }

    @staticmethod
    def _restore_day(day, body):
        """Reject malformed saved observations and hours outside the requested UTC day."""
        start = datetime.combine(day, datetime.min.time(), UTC)
        if not isinstance(body, list) or len(body) > HOURS_PER_DAY:
            raise ValueError("Invalid cached temperature day")
        rows = []
        previous_hour = -1
        for hour, celsius, known_at in body:
            if type(hour) is not int or not previous_hour < hour < HOURS_PER_DAY:
                raise ValueError("Invalid cached temperature hour")
            rows.append(
                TemperatureHour(start + hour * HOUR, celsius, datetime.fromisoformat(known_at))
            )
            previous_hour = hour
        return tuple(rows)

    def save(self, first, last, rows, previous):
        """Replace corrected values while retaining accepted hours omitted by the provider."""
        by_day = {
            day: {row.start: row for row in previous.get(day, ())}
            for day in (first + offset * DAY for offset in range((last - first).days + 1))
        }
        for row in rows:
            hours = by_day.get(row.start.date())
            if hours is not None:
                original = hours.get(row.start)
                if original is None or original.celsius != row.celsius:
                    hours[row.start] = row
        changed = {}
        for day, hours in by_day.items():
            accepted = tuple(hours[start] for start in sorted(hours))
            if accepted != previous.get(day):
                changed[day] = accepted
        if changed:
            with self.documents.transaction():
                for day, hours in changed.items():
                    self.documents.write(
                        self.prefix + day.isoformat() + ".json",
                        [[row.start.hour, row.celsius, row.known_at.isoformat()] for row in hours],
                    )
        return {**previous, **changed}


class OpenMeteoTemperatureSource:
    """Fetch Celsius temperatures for HA's location with bounded requests and daily historical caching."""

    def __init__(self, session, documents, history_start=None):
        """Borrow the app's HTTP session and weather archive."""
        self.session = session
        self.documents = documents
        self.history_start = history_start
        self.past_key, self.past = None, ()

    async def read(self, location, now):
        """Return completed historical hours, retaining receipt as the knowledge time.

        Newly downloaded past weather was not necessarily available at an old
        decision time. Backtests must filter known_at rather than silently treat
        reanalysis as an archived historical forecast.
        """
        now = aware(now)
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
            first = min(first, aware(measured_start).date())
        key = location.latitude, location.longitude, now.date(), first
        if key != self.past_key:
            end = now.date() - timedelta(days=ARCHIVE_WEATHER_LAG_DAYS)
            past = []
            for year in range(first.year, end.year + 1):
                days = await self._archive_days(
                    params,
                    max(first, datetime(year, 1, 1).date()),
                    min(end, datetime(year, 12, 31).date()),
                    now,
                )
                past.extend(row for day in sorted(days) for row in days[day])
            self.past_key, self.past = key, tuple(past)
        first_recent = now.date() - timedelta(days=RECENT_WEATHER_DAYS)
        recent = TemperatureDays(self.documents, RECENT_URL, params)
        recent_days = await asyncio.to_thread(recent.read, first_recent, now.date())
        refreshed = await self._refresh(
            recent, RECENT_URL, params, first_recent, now.date(), now, recent_days
        )
        return self.past + tuple(
            row for day in sorted(refreshed) for row in refreshed[day] if row.start + HOUR <= now
        )

    async def _archive_days(self, params, first, last, now):
        """Load cached days and request contiguous missing or incomplete archive dates together."""
        cache = TemperatureDays(self.documents, ARCHIVE_URL, params)
        days = await asyncio.to_thread(cache.read, first, last)
        missing = [
            day
            for day in (first + offset * DAY for offset in range((last - first).days + 1))
            if len(days.get(day, ())) < HOURS_PER_DAY
        ]
        for _, group in groupby(enumerate(missing), lambda item: item[1].toordinal() - item[0]):
            dates = tuple(day for _, day in group)
            days = await self._refresh(cache, ARCHIVE_URL, params, dates[0], dates[-1], now, days)
        return days

    async def _refresh(self, cache, url, params, first, last, now, previous):
        """Commit only completed hours after the provider response has passed validation."""
        rows = await self._read(
            url, {**params, "start_date": first.isoformat(), "end_date": last.isoformat()}, now
        )
        completed = tuple(row for row in rows if row.start + HOUR <= now)
        result = {}

        def save():
            """Publish the committed day map once storage completes off the event loop."""
            result.update(cache.save(first, last, completed, previous))

        cancellation = await complete_write(save)
        if cancellation:
            raise cancellation
        return result

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
async def weather_factory(documents, history_start=None):
    """Share one provider session and durable historical cache across all learners."""
    async with ClientSession() as session:
        yield OpenMeteoTemperatureSource(session, documents, history_start=history_start)
