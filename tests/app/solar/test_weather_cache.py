"""Prove complete historical weather caching across restarts without changing knowledge times."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.storage.weather import HistoricalWeatherCache
from open_energy_planner.solar.weather import ARCHIVE_URL, OpenMeteoSolarSource
from tests.app.demand.test_demand_weather import Response
from tests.app.solar.support import SITE
from tests.database.support.archive import list_documents, make_store

NOW = datetime(2026, 6, 20, 12, tzinfo=UTC)
UNITS = {
    "shortwave_radiation": "W/m²",
    "direct_normal_irradiance": "W/m²",
    "diffuse_radiation": "W/m²",
    "temperature_2m": "°C",
    "wind_speed_10m": "m/s",
}
PARAMS = {
    "latitude": 52.0,
    "longitude": 6.0,
    "hourly": ",".join(UNITS),
    "timezone": "UTC",
    "timeformat": "unixtime",
    "wind_speed_unit": "ms",
}


class MonthlySession:
    """Serve exact provider calendar hours and independently damage optional history suffixes."""

    def __init__(self):
        """Retain request arguments and a controllable response failure without external network traffic."""
        self.calls, self.damage = [], None

    def get(self, url, **kwargs):
        """Return end-labelled hours with complete units, or a chosen malformed historical result."""
        params = kwargs["params"]
        self.calls.append((url, dict(params)))
        archive = url == ARCHIVE_URL
        if archive and self.damage == "failure":
            raise OSError("Synthetic optional historical outage")
        if archive:
            first = datetime.combine(
                date.fromisoformat(params["start_date"]), datetime.min.time(), UTC
            )
            end = datetime.combine(
                date.fromisoformat(params["end_date"]) + timedelta(days=1), datetime.min.time(), UTC
            )
            count = int((end - first) / timedelta(hours=1))
        else:
            first, count = NOW, 2
        hours = {
            key: [value] * count for key, value in zip(UNITS, (500, 600, 100, 15, 2), strict=True)
        }
        hours["time"] = [(first + timedelta(hours=index)).timestamp() for index in range(count)]
        units = dict(UNITS)
        if archive and self.damage == "missing":
            hours["shortwave_radiation"][0] = None
        if archive and self.damage == "short":
            hours = {key: values[:-1] for key, values in hours.items()}
        if archive and self.damage == "bad_unit":
            units["wind_speed_10m"] = "km/h"
        if archive and self.damage == "shift":
            hours["time"] = [stamp + 3600 for stamp in hours["time"]]
        return Response(json.dumps({"hourly_units": units, "hourly": hours}).encode())


def archive_calls(session):
    """Read only historical request dates while leaving independent forecast refreshes out of assertions."""
    return [params for url, params in session.calls if url == ARCHIVE_URL]


def documents(store, directory):
    """Snapshot durable solar JSON through the real database to detect unexpected evidence rewrites."""
    return {name: store.documents.read(name) for name in list_documents(directory, "solar")}


def test_restart_reuses_closed_month_and_partial_prefix_without_historical_http(
    tmp_path, monkeypatch
):
    """May and June's complete prefix survive a new store/source while current forecasts still refresh."""
    monkeypatch.setattr("open_energy_planner.solar.weather.ARCHIVE_LOOKBACK_DAYS", 50)

    async def scenario():
        """Compare restarted transport requests, durable documents and original normalized receipts."""
        first_store = make_store(tmp_path, SolarStore)
        first_session = MonthlySession()
        first = await OpenMeteoSolarSource(first_session, first_store).read_observed(SITE, NOW)
        calls = archive_calls(first_session)
        assert [(call["start_date"], call["end_date"]) for call in calls] == [
            ("2026-05-01", "2026-05-31"),
            ("2026-06-01", "2026-06-12"),
        ]
        assert all(call["timezone"] == "UTC" and call["wind_speed_unit"] == "ms" for call in calls)
        snapshot = documents(first_store, tmp_path)
        second_session = MonthlySession()
        second_store = make_store(tmp_path, SolarStore)
        second = await OpenMeteoSolarSource(second_session, second_store).read_observed(
            SITE, NOW + timedelta(hours=1)
        )
        assert archive_calls(second_session) == []
        assert len(second_session.calls) == 1
        old_hours = {
            row.start: row for row in first if row.start < datetime(2026, 6, 13, tzinfo=UTC)
        }
        assert {row.start: row for row in second if row.start in old_hours} == old_hours
        assert all(row.received_at == NOW for row in second if row.start in old_hours)
        assert documents(second_store, tmp_path) == snapshot

    asyncio.run(scenario())


def test_partial_month_extends_only_missing_dates_and_reuses_prior_value_blocks(
    tmp_path, monkeypatch
):
    """The next day fetches June13 only and retains May plus June1-12 with their original receipt time."""
    monkeypatch.setattr("open_energy_planner.solar.weather.ARCHIVE_LOOKBACK_DAYS", 50)

    async def scenario():
        """Extend durable coverage through one complete suffix without copying old value blocks."""
        store = make_store(tmp_path, SolarStore)
        await OpenMeteoSolarSource(MonthlySession(), store).read_observed(SITE, NOW)
        original = documents(store, tmp_path)
        later_session = MonthlySession()
        later = await OpenMeteoSolarSource(
            later_session, make_store(tmp_path, SolarStore)
        ).read_observed(SITE, NOW + timedelta(days=1))
        assert [
            (call["start_date"], call["end_date"]) for call in archive_calls(later_session)
        ] == [("2026-06-13", "2026-06-13")]
        old = next(row for row in later if row.start == datetime(2026, 6, 11, tzinfo=UTC))
        added = next(row for row in later if row.start == datetime(2026, 6, 13, 12, tzinfo=UTC))
        assert old.received_at == NOW
        assert added.received_at == NOW + timedelta(days=1)
        retained = documents(store, tmp_path)
        receipt = next(
            value
            for name, value in retained.items()
            if name.startswith("weather-cache-receipt-")
            and value["query"]["params"]["start_date"] == "2026-06-01"
            and value["query"]["params"]["end_date"] == "2026-06-13"
        )
        assert [
            (
                request["query"]["params"]["start_date"],
                request["query"]["params"]["end_date"],
                request["received_at"],
            )
            for request in receipt["requests"]
        ] == [
            ("2026-06-01", "2026-06-12", NOW.isoformat()),
            ("2026-06-13", "2026-06-13", (NOW + timedelta(days=1)).isoformat()),
        ]
        for name, value in original.items():
            if not name.startswith("weather-cache-index-"):
                assert retained[name] == value
        assert (
            len([name for name in retained if name.startswith("block-")])
            - len([name for name in original if name.startswith("block-")])
            == 2
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["missing", "short", "bad_unit", "shift", "failure"])
def test_incomplete_or_failed_suffix_cannot_advance_coverage(tmp_path, monkeypatch, damage):
    """Malformed or missing June13 data keeps the old prefix and retries the same suffix on recovery."""
    monkeypatch.setattr("open_energy_planner.solar.weather.ARCHIVE_LOOKBACK_DAYS", 50)

    async def scenario():
        """Preserve all original documents on failure, then admit only a complete corrected response."""
        store = make_store(tmp_path, SolarStore)
        await OpenMeteoSolarSource(MonthlySession(), store).read_observed(SITE, NOW)
        original = documents(store, tmp_path)
        session = MonthlySession()
        session.damage = damage
        source = OpenMeteoSolarSource(session, store)
        retained = await source.read_observed(SITE, NOW + timedelta(days=1))
        assert any(row.start == datetime(2026, 6, 11, tzinfo=UTC) for row in retained)
        assert any(row.start == NOW - timedelta(hours=1) for row in retained)
        assert documents(store, tmp_path) == original
        session.damage = None
        result = await source.read_observed(SITE, NOW + timedelta(days=1))
        assert [(call["start_date"], call["end_date"]) for call in archive_calls(session)] == [
            ("2026-06-13", "2026-06-13")
        ] * 2
        assert any(row.start == datetime(2026, 6, 13, 12, tzinfo=UTC) for row in result)

    asyncio.run(scenario())


def test_site_change_does_not_reuse_another_locations_history(tmp_path, monkeypatch):
    """Identical dates at another latitude require independent historical requests and cache identities."""
    monkeypatch.setattr("open_energy_planner.solar.weather.ARCHIVE_LOOKBACK_DAYS", 50)

    async def scenario():
        """Exercise production request identity rather than relying on manually constructed key equality."""
        store = make_store(tmp_path, SolarStore)
        await OpenMeteoSolarSource(MonthlySession(), store).read_observed(SITE, NOW)
        session = MonthlySession()
        await OpenMeteoSolarSource(session, store).read_observed(
            replace(SITE, latitude=SITE.latitude + 1), NOW
        )
        assert len(archive_calls(session)) == 2
        assert all(call["latitude"] == SITE.latitude + 1 for call in archive_calls(session))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"models": "another-model"},
        {"run": "2026-06-01T00:00"},
        {"timezone": "Europe/Amsterdam"},
        {"hourly": "temperature_2m"},
        {"wind_speed_unit": "kmh"},
    ],
)
def test_cache_identity_binds_every_result_parameter(tmp_path, change):
    """Future model/run metadata cannot hit a stored query with different result-affecting parameters."""
    from open_energy_planner.solar.calculation.model import SolarWeatherHour

    cache = HistoricalWeatherCache(make_store(tmp_path, SolarStore))
    month = date(2026, 6, 1)
    rows = tuple(
        SolarWeatherHour(
            datetime(2026, 5, 31, 23, tzinfo=UTC) + timedelta(hours=index),
            500,
            600,
            100,
            15,
            2,
            NOW,
        )
        for index in range(24)
    )
    cache.write(ARCHIVE_URL, PARAMS, month, month, rows)
    assert cache.read(ARCHIVE_URL, PARAMS, month) == (rows, month)
    assert cache.read(ARCHIVE_URL, {**PARAMS, **change}, month) == ((), date(2026, 5, 31))
    assert cache.read("https://example.invalid/other", PARAMS, month) == ((), date(2026, 5, 31))


def test_changed_normalization_does_not_reuse_old_values(tmp_path, monkeypatch):
    """An adapter normalization revision gets a separate cache family even at the same site and dates."""
    from open_energy_planner.solar.calculation.model import SolarWeatherHour

    cache = HistoricalWeatherCache(make_store(tmp_path, SolarStore))
    month = date(2026, 6, 1)
    rows = tuple(
        SolarWeatherHour(
            datetime(2026, 5, 31, 23, tzinfo=UTC) + timedelta(hours=index),
            500,
            600,
            100,
            15,
            2,
            NOW,
        )
        for index in range(24)
    )
    cache.write(ARCHIVE_URL, PARAMS, month, month, rows)
    monkeypatch.setattr(
        "open_energy_planner.solar.storage.weather.WEATHER_NORMALIZATION_REVISION",
        "synthetic-changed-normalization",
    )
    assert cache.read(ARCHIVE_URL, PARAMS, month) == ((), date(2026, 5, 31))


def test_changed_cached_values_or_receipts_cannot_rewrite_a_retained_prefix(tmp_path):
    """An extension with changed old irradiance or refreshed old receipt times fails without modifying documents."""
    from open_energy_planner.solar.calculation.model import SolarWeatherHour

    store = make_store(tmp_path, SolarStore)
    cache = HistoricalWeatherCache(store)
    month = date(2026, 6, 1)
    rows = tuple(
        SolarWeatherHour(
            datetime(2026, 5, 31, 23, tzinfo=UTC) + timedelta(hours=index),
            500,
            600,
            100,
            15,
            2,
            NOW,
        )
        for index in range(48)
    )
    cache.write(ARCHIVE_URL, PARAMS, month, month, rows[:24])
    original = documents(store, tmp_path)
    for changes in ({"ghi": 400}, {"received_at": NOW + timedelta(hours=1)}):
        damaged = (replace(rows[0], **changes), *rows[1:])
        with pytest.raises(ValueError, match="prefix changed"):
            cache.write(ARCHIVE_URL, PARAMS, month, date(2026, 6, 2), damaged)
        assert documents(store, tmp_path) == original


def test_newly_loaded_older_history_fetches_only_missing_months_without_an_age_cutoff(
    tmp_path, monkeypatch
):
    """A loaded 2024 measurement expands a 2026 cohort without refetching cached recent months or oversized transfers."""
    monkeypatch.setattr("open_energy_planner.solar.weather.ARCHIVE_LOOKBACK_DAYS", 50)

    async def scenario():
        """Freeze the cohort before transport, then notice older loaded history on the next refresh."""
        oldest = [None]

        class LoadingSession(MonthlySession):
            """Expose an older measurement during transport to verify cohort snapshot semantics."""

            def get(self, url, **kwargs):
                """Change loaded history once after the first read has already captured its starting range."""
                result = super().get(url, **kwargs)
                if url != ARCHIVE_URL:
                    oldest[0] = datetime(2024, 6, 1, tzinfo=UTC)
                return result

        store = make_store(tmp_path, SolarStore)
        session = LoadingSession()
        source = OpenMeteoSolarSource(session, store, history_start=lambda: oldest[0])
        await source.read_observed(SITE, NOW)
        assert len(archive_calls(session)) == 2
        session.calls.clear()
        expanded = await source.read_observed(SITE, NOW)
        calls = archive_calls(session)
        assert calls[0]["start_date"] == "2024-05-01"
        assert calls[-1]["end_date"] == "2026-04-30"
        assert all(call["end_date"] < "2026-05-01" for call in calls)
        assert all(
            (date.fromisoformat(call["end_date"]) - date.fromisoformat(call["start_date"])).days
            < 31
            for call in calls
        )
        assert len(expanded) > 2 * 365 * 24
        assert min(row.start for row in expanded) == datetime(2024, 5, 30, 23, tzinfo=UTC)
        session.calls.clear()
        await source.read_observed(SITE, NOW)
        assert archive_calls(session) == []
        restarted = MonthlySession()
        await OpenMeteoSolarSource(
            restarted, make_store(tmp_path, SolarStore), history_start=lambda: oldest[0]
        ).read_observed(SITE, NOW)
        assert archive_calls(restarted) == []

    asyncio.run(scenario())


def test_corrupt_cached_receipt_is_retained_without_blocking_independent_forecast(
    tmp_path, monkeypatch
):
    """Changed retained receipt metadata cannot become accepted weather or erase other complete cached months."""
    monkeypatch.setattr("open_energy_planner.solar.weather.ARCHIVE_LOOKBACK_DAYS", 50)

    async def scenario():
        """Keep corruption observable instead of replacing its original evidence with an unverified cache hit."""
        store = make_store(tmp_path, SolarStore)
        await OpenMeteoSolarSource(MonthlySession(), store).read_observed(SITE, NOW)
        pointer = next(
            name
            for name in list_documents(tmp_path, "solar")
            if name.startswith("weather-cache-index-")
            and store.documents.read(f"weather-cache-receipt-{store.documents.read(name)}.json")[
                "query"
            ]["params"]["start_date"]
            == "2026-05-01"
        )
        reference = store.documents.read(pointer)
        path = f"weather-cache-receipt-{reference}.json"
        corrupt = store.documents.read(path)
        corrupt["requests"][0]["received_at"] = "2026-01-01T00:00:00+00:00"
        store.documents.write(path, corrupt)
        session = MonthlySession()
        result = await OpenMeteoSolarSource(session, store).read_observed(SITE, NOW)
        assert any(row.start == NOW - timedelta(hours=1) for row in result)
        assert any(row.start == datetime(2026, 6, 11, tzinfo=UTC) for row in result)
        assert not any(row.start < datetime(2026, 5, 31, 23, tzinfo=UTC) for row in result)
        assert store.documents.read(path) == corrupt
        assert archive_calls(session) == []

    asyncio.run(scenario())


def test_multiyear_archive_outage_tries_once_and_keeps_later_cached_months(tmp_path, monkeypatch):
    """A failed older request cannot multiply timeout waits by all missing months or hide cached recent years."""
    monkeypatch.setattr("open_energy_planner.solar.weather.ARCHIVE_LOOKBACK_DAYS", 50)

    async def scenario():
        """Expand the measurement cohort during a provider outage without rewriting accepted receipts."""
        store = make_store(tmp_path, SolarStore)
        accepted = await OpenMeteoSolarSource(MonthlySession(), store).read_observed(SITE, NOW)
        snapshot = documents(store, tmp_path)
        session = MonthlySession()
        session.damage = "failure"
        result = await OpenMeteoSolarSource(
            session, store, history_start=lambda: datetime(2020, 1, 1, tzinfo=UTC)
        ).read_observed(SITE, NOW)
        assert len(archive_calls(session)) == 1
        assert len(session.calls) == 2
        assert set(result) == set(accepted)
        assert documents(store, tmp_path) == snapshot

    asyncio.run(scenario())
