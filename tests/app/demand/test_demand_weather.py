"""Bounded replaceable weather-adapter fixtures. Never contact the external provider in tests."""

import asyncio
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from open_energy_planner.weather.history import OpenMeteoTemperatureSource
from tests.app.demand.test_demand import START


@pytest.fixture
def weather_documents(tmp_path):
    """Open the same durable weather namespace used by the historical temperature service."""
    with closing(SqlPlanRepository(tmp_path)) as repository:
        yield SqlDocuments(repository.engine, "weather")


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
                    "time": [START.timestamp() - 3600, START.timestamp()],
                    "temperature_2m": [5, None],
                },
            }
        ).encode()
        return Response(b"x" * (2 * 1024 * 1024 + 1) if self.oversized else raw)


class AnnualSession:
    """Return complete synthetic Celsius data within each requested annual interval."""

    def __init__(self):
        """Keep exact requests and one controlled archive failure without external traffic."""
        self.calls = []
        self.failure_start = None
        self.recent_offset = 0

    def get(self, url, **kwargs):
        """Serve every requested historical hour with a location-specific Celsius value."""
        params = kwargs["params"]
        self.calls.append((url, dict(params)))
        if self.failure_start and params.get("start_date") == self.failure_start:
            raise OSError("Synthetic archive failure")
        start = datetime.fromisoformat(params.get("start_date", "2026-06-01")).replace(tzinfo=UTC)
        end = datetime.fromisoformat(params.get("end_date", "2026-06-02")).replace(
            tzinfo=UTC
        ) + timedelta(days=1)
        times = [
            int((start + timedelta(hours=index)).timestamp())
            for index in range(int((end - start).total_seconds() / 3600))
        ]
        assert len(times) <= 9000
        temperature = params["latitude"] - 47
        if url.endswith("/forecast"):
            temperature += self.recent_offset
        return Response(
            json.dumps(
                {
                    "hourly_units": {"temperature_2m": "°C"},
                    "hourly": {
                        "time": times,
                        "temperature_2m": [temperature] * len(times),
                    },
                }
            ).encode()
        )


def test_weather_adapter_uses_ha_location_units_receipt_time_and_daily_archive_cache(
    weather_documents,
):
    """Use HA coordinates and keep a missing temperature absent on repeated reads."""

    async def scenario():
        """Exercise real adapter normalization with bounded fake HTTP, without external traffic."""
        session = Session()
        source = OpenMeteoTemperatureSource(session, weather_documents)
        location = SiteLocation(52, 6, "Europe/Amsterdam")
        result = await source.read(location, START)
        assert len(result) == 1
        assert result[0].celsius == 5
        assert result[0].known_at == START
        await source.read(location, START)
        assert len(session.calls) == 4
        assert session.calls[0][1]["params"]["latitude"] == 52
        assert session.calls[0][1]["params"]["longitude"] == 6
        assert session.calls[2][1]["params"]["end_date"] == START.date().isoformat()

    asyncio.run(scenario())


def test_historical_temperature_includes_completed_hours_on_the_present_day(weather_documents):
    """A completed midday load sample has weather while the ongoing hour stays excluded."""
    now = START.replace(hour=12, minute=15)
    session = AnnualSession()
    source = OpenMeteoTemperatureSource(session, weather_documents)
    rows = asyncio.run(source.read(SiteLocation(52, 6, "Europe/Amsterdam"), now))
    today = [row.start.hour for row in rows if row.start.date() == now.date()]
    assert today == list(range(12))


@pytest.mark.parametrize("options", [{"unit": "°F"}, {"oversized": True}])
def test_weather_adapter_rejects_wrong_units_and_unbounded_responses(options, weather_documents):
    """Fahrenheit or an oversized provider response cannot silently become usable Celsius model input."""

    async def scenario():
        """Reject malformed provider evidence before it reaches the forecaster."""
        with pytest.raises(ValueError):
            await OpenMeteoTemperatureSource(Session(**options), weather_documents).read(
                SiteLocation(52, 6, "Europe/Amsterdam"), START
            )

    asyncio.run(scenario())


def test_temperature_history_covers_all_loaded_years_with_bounded_cached_queries(weather_documents):
    """Older consumer history extends weather requests without enlarging a response or backdating receipt."""

    async def scenario():
        """Generate one independent real-hour response per query and retain closed years on refresh."""

        session = AnnualSession()
        source = OpenMeteoTemperatureSource(
            session, weather_documents, history_start=lambda: datetime(2022, 3, 1, tzinfo=UTC)
        )
        now = datetime(2026, 6, 1, tzinfo=UTC)
        location = SiteLocation(52, 6, "Europe/Amsterdam")
        hours = await source.read(location, now)
        assert hours[0].start == datetime(2022, 3, 1, tzinfo=UTC)
        assert len(hours) > 3 * 365 * 24
        assert all(hour.known_at == now for hour in hours)
        assert len(session.calls) == 6
        updated = await source.read(location, now + timedelta(days=1))
        assert len(session.calls) == 8
        assert session.calls[-2][1]["start_date"] == "2026-05-25"
        assert session.calls[-2][1]["end_date"] == "2026-05-25"
        assert updated[0] == hours[0]

    asyncio.run(scenario())


def test_temperature_cache_remains_bounded_as_the_daily_lookback_rolls(weather_documents):
    """Rolling historical coverage fetches only newly needed archive days after the first read."""

    async def scenario():
        """Independently check the entire returned time coverage while the previous-year start advances."""
        session = AnnualSession()
        source = OpenMeteoTemperatureSource(session, weather_documents)
        location = SiteLocation(52, 6, "Europe/Amsterdam")
        now = datetime(2026, 6, 1, tzinfo=UTC)
        for offset in range(5):
            current = now + timedelta(days=offset)
            hours = await source.read(location, current)
            first = current - timedelta(days=367)
            end = current
            expected = {
                first + timedelta(hours=index)
                for index in range(int((end - first).total_seconds() / 3600))
            }
            assert {hour.start for hour in hours} == expected
            assert all(hour.celsius == 5 and hour.known_at <= current for hour in hours)
        assert len(session.calls) == 11
        assert all(params["start_date"] == params["end_date"] for _, params in session.calls[3::2])

    asyncio.run(scenario())


def test_temperature_cache_tracks_history_start_and_location_without_losing_annual_reuse(
    weather_documents,
):
    """Extend and shorten historical coverage, reuse unchanged annual requests and isolate site temperatures."""

    async def scenario():
        """Change the existing history callback and HA coordinates while observing actual bounded requests."""
        session = AnnualSession()
        history_start = datetime(2022, 3, 1, tzinfo=UTC)
        source = OpenMeteoTemperatureSource(
            session, weather_documents, history_start=lambda: history_start
        )
        now = datetime(2026, 6, 1, tzinfo=UTC)
        location = SiteLocation(52, 6, "Europe/Amsterdam")
        await source.read(location, now)
        history_start = datetime(2024, 3, 1, tzinfo=UTC)
        calls_before = len(session.calls)
        shortened = await source.read(location, now)
        assert shortened[0].start == history_start
        assert len(session.calls) - calls_before == 1
        history_start = datetime(2022, 3, 1, tzinfo=UTC)
        extended = await source.read(location, now)
        assert extended[0].start == history_start
        changed = await source.read(SiteLocation(53, 6, "Europe/Amsterdam"), now)
        assert {hour.start for hour in changed} == {hour.start for hour in extended}
        assert all(hour.celsius == 6 for hour in changed)
        assert len(session.calls) - calls_before == 8

    asyncio.run(scenario())


def test_temperature_archive_failure_keeps_last_complete_history_and_bounds_partial_retries(
    weather_documents,
):
    """A failed archive refresh preserves accepted history and retries the outstanding days."""

    async def scenario():
        """Fail the current-year fetch after the preceding annual response, then recover the same request."""
        session = AnnualSession()
        source = OpenMeteoTemperatureSource(session, weather_documents)
        now = datetime(2026, 6, 1, tzinfo=UTC)
        location = SiteLocation(52, 6, "Europe/Amsterdam")
        await source.read(location, now)
        accepted, accepted_key = source.past, source.past_key
        session.failure_start = "2026-05-25"
        for offset in (1, 2):
            with pytest.raises(OSError, match="Synthetic archive failure"):
                await source.read(location, now + timedelta(days=offset))
            assert source.past == accepted and source.past_key == accepted_key
        session.failure_start = None
        calls_before = len(session.calls)
        recovered = await source.read(location, now + timedelta(days=2))
        assert len(session.calls) - calls_before == 2
        assert source.past_key != accepted_key
        assert recovered[0].start == now + timedelta(days=2 - 367)
        assert all(hour.celsius == 5 for hour in recovered)
        assert session.calls[-2][1]["start_date"] == "2026-05-25"
        assert session.calls[-2][1]["end_date"] == "2026-05-26"

    asyncio.run(scenario())


def test_cached_training_temperatures_survive_restart_and_refresh_in_place(weather_documents):
    """A restarted provider reuses archived days and replaces corrected recent observations."""

    async def scenario():
        """Reload real persisted observations through a separate provider and document reader."""
        first = AnnualSession()
        location = SiteLocation(52, 6, "Europe/Amsterdam")
        now = datetime(2026, 6, 1, 12, tzinfo=UTC)
        original = await OpenMeteoTemperatureSource(first, weather_documents).read(location, now)
        first_count = len(list(weather_documents.iter_prefix("temperature-day-")))
        second = AnnualSession()
        reopened = SqlDocuments(weather_documents.engine, "weather")
        source = OpenMeteoTemperatureSource(second, reopened)
        assert await source.read(location, now) == original
        assert [url.rsplit("/", 1)[-1] for url, _ in second.calls] == ["forecast"]
        second.recent_offset = 2
        refreshed = await source.read(location, now + timedelta(minutes=30))
        assert refreshed[0] == original[0]
        assert refreshed[-1].celsius == 7
        assert refreshed[-1].known_at == now + timedelta(minutes=30)
        assert len(list(reopened.iter_prefix("temperature-day-"))) == first_count

    asyncio.run(scenario())
