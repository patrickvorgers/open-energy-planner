"""Verify original-run weather and durable evidence without backdating retrieval or contacting providers."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, datetime, timedelta
from threading import Event as ThreadEvent

import pytest
from aiohttp import ClientResponseError

from open_energy_planner.solar.calculation.model import version
from open_energy_planner.solar.forecast_weather import ForecastWeatherRun
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.storage.forecast_weather import ForecastWeatherCache
from open_energy_planner.solar.weather import OpenMeteoSolarSource, forecast_weather_query
from tests.app.demand.test_demand_weather import Response
from tests.app.solar.support import SITE
from tests.database.support.archive import list_documents, make_store

INITIALIZED = datetime(2026, 4, 1, 12, tzinfo=UTC)
RECEIVED = datetime(2026, 11, 8, 17, tzinfo=UTC)
UNITS = {
    "shortwave_radiation": "W/m²",
    "direct_normal_irradiance": "W/m²",
    "diffuse_radiation": "W/m²",
    "temperature_2m": "°C",
    "wind_speed_10m": "m/s",
}


class FrozenRunSession:
    """Serve independently specified fixed initialization hours and controllable provider defects."""

    def __init__(self, damage=None):
        """Select a failure while retaining exact HTTP arguments for provenance assertions."""
        self.calls, self.damage = [], damage

    def get(self, url, **kwargs):
        """Produce end-labelled original-run intervals, including a legitimately null initialization radiation."""
        self.calls.append((url, kwargs))
        if self.damage == "failure":
            raise OSError("Synthetic frozen forecast outage")
        params = kwargs["params"]
        initialized = datetime.fromisoformat(params["run"]).replace(tzinfo=UTC)
        count = 96
        hourly = {
            key: [value + index * 0.01 for index in range(count)]
            for key, value in zip(UNITS, (400, 500, 100, 15, 2), strict=True)
        }
        for field in list(UNITS)[:3]:
            hourly[field][0] = None
        hourly["time"] = [
            (initialized + timedelta(hours=index)).timestamp() for index in range(count)
        ]
        units = dict(UNITS)
        if self.damage == "required_gap":
            hourly["shortwave_radiation"][30] = None
        if self.damage == "late_gap":
            hourly["shortwave_radiation"][90] = None
        if self.damage == "missing_neighbor":
            hourly["shortwave_radiation"][73] = None
        if self.damage == "shift":
            hourly["time"] = [stamp + 3600 for stamp in hourly["time"]]
        if self.damage == "bad_units":
            units["wind_speed_10m"] = "km/h"
        if self.damage == "short":
            hourly = {field: values[:49] for field, values in hourly.items()}
        if self.damage == "duplicate":
            hourly["time"][40] = hourly["time"][39]
        if self.damage == "too_many":
            hourly = {field: [values[-1]] * 9001 for field, values in hourly.items()}
        raw = json.dumps({"hourly_units": units, "hourly": hourly}).encode()
        if self.damage == "oversize":
            raw = b" " * (4 * 1024 * 1024 + 1)
        return Response(raw)


def snapshot(store, path):
    """Read every retained solar document to prove failed or repeated reads cannot rewrite evidence."""
    return {name: store.documents.read(name) for name in list_documents(path, "solar")}


def test_fixed_run_query_and_normalization_keep_init_origin_retrieval_and_unknown_publication_separate():
    """A public original IFS request yields 95 normalized hours and a simulated origin without fictitious availability."""
    session = FrozenRunSession()
    run = asyncio.run(OpenMeteoSolarSource(session).read_run(SITE, INITIALIZED, None, RECEIVED))
    assert len(session.calls) == 1
    url, options = session.calls[0]
    assert url == "https://single-runs-api.open-meteo.com/v1/forecast"
    assert options["allow_redirects"] is False
    assert options["params"] == {
        "latitude": SITE.latitude,
        "longitude": SITE.longitude,
        "hourly": "shortwave_radiation,direct_normal_irradiance,diffuse_radiation,temperature_2m,wind_speed_10m",
        "timezone": "UTC",
        "timeformat": "unixtime",
        "wind_speed_unit": "ms",
        "models": "ecmwf_ifs",
        "run": "2026-04-01T12:00",
        "forecast_hours": 96,
    }
    assert "apikey" not in options["params"]
    assert run.model == "ecmwf_ifs"
    assert run.initialized_at == INITIALIZED
    assert run.forecast_origin == INITIALIZED + timedelta(days=1)
    assert run.received_at == RECEIVED
    assert run.available_at is None
    assert len(run.hours) == 95
    assert run.hours[0].start == INITIALIZED
    assert run.hours[-1].start == INITIALIZED + timedelta(hours=94)
    assert run.hours[0].ghi == pytest.approx(400.01)
    assert all(row.received_at == RECEIVED for row in run.hours)
    assert run.forecast_origin < run.received_at


def test_cache_restart_and_multiple_origins_reuse_frozen_values_without_any_http(tmp_path):
    """Day-zero and day-one simulations reopen one database run with its original real retrieval clock."""
    store = make_store(tmp_path, SolarStore)
    first_session = FrozenRunSession()
    first = asyncio.run(
        OpenMeteoSolarSource(first_session, store).read_run(SITE, INITIALIZED, None, RECEIVED)
    )
    saved = snapshot(store, tmp_path)
    restarted_store = make_store(tmp_path, SolarStore)
    failing_session = FrozenRunSession("failure")
    restarted = OpenMeteoSolarSource(failing_session, restarted_store)
    for origin in (INITIALIZED, INITIALIZED + timedelta(days=1), INITIALIZED + timedelta(hours=30)):
        run = asyncio.run(
            restarted.read_run(SITE, INITIALIZED, origin, RECEIVED + timedelta(days=1))
        )
        assert run.forecast_origin == origin
        assert run.hours == first.hours
        assert run.received_at == RECEIVED
        assert run.available_at is None
    assert failing_session.calls == []
    assert snapshot(restarted_store, tmp_path) == saved
    assert len([name for name in saved if name.startswith("forecast-weather-index-")]) == 1
    assert len([name for name in saved if name.startswith("forecast-weather-receipt-")]) == 1


def test_site_model_run_and_query_normalization_are_separate_cache_identities(tmp_path):
    """A neighboring site, another initialization or altered model/units cannot reuse the retained request."""
    store = make_store(tmp_path, SolarStore)
    source = OpenMeteoSolarSource(FrozenRunSession(), store)
    original = asyncio.run(source.read_run(SITE, INITIALIZED, None, RECEIVED))
    cache = ForecastWeatherCache(store)
    query = forecast_weather_query(SITE, INITIALIZED)
    assert cache.read(query) == original
    for field, value in (("models", "icon_eu"), ("wind_speed_unit", "kmh")):
        changed = deepcopy(query)
        changed["params"][field] = value
        assert cache.read(changed) is None
    changed = deepcopy(query)
    changed["normalization"] = "another-normalization"
    assert cache.read(changed) is None
    assert (
        cache.read(forecast_weather_query(replace(SITE, longitude=SITE.longitude + 1), INITIALIZED))
        is None
    )
    assert cache.read(forecast_weather_query(SITE, INITIALIZED + timedelta(hours=6))) is None
    different_site_session = FrozenRunSession()
    different_site = asyncio.run(
        OpenMeteoSolarSource(different_site_session, store).read_run(
            replace(SITE, longitude=SITE.longitude + 1), INITIALIZED, None, RECEIVED
        )
    )
    assert different_site.location_version != original.location_version
    assert len(different_site_session.calls) == 1


@pytest.mark.parametrize(
    "damage",
    [
        "required_gap",
        "missing_neighbor",
        "bad_units",
        "short",
        "duplicate",
        "too_many",
        "oversize",
        "failure",
    ],
)
def test_failed_or_partial_required_horizon_cannot_install_a_cache_pointer(tmp_path, damage):
    """Missing required forecast hours, malformed transport and outages remain explicit without fake complete evidence."""
    store = make_store(tmp_path, SolarStore)
    before = snapshot(store, tmp_path)
    with pytest.raises((ValueError, OSError)):
        asyncio.run(
            OpenMeteoSolarSource(FrozenRunSession(damage), store).read_run(
                SITE, INITIALIZED, None, RECEIVED
            )
        )
    assert snapshot(store, tmp_path) == before


def test_a_gap_beyond_the_required_window_preserves_usable_forecast_without_false_complete_cache(
    tmp_path,
):
    """A valid immediate 48-hour horizon remains usable when a later optional day has missing radiation."""
    store = make_store(tmp_path, SolarStore)
    session = FrozenRunSession("late_gap")
    run = asyncio.run(
        OpenMeteoSolarSource(session, store).read_run(SITE, INITIALIZED, INITIALIZED, RECEIVED)
    )
    starts = {row.start for row in run.hours}
    assert all(INITIALIZED + timedelta(hours=index) in starts for index in range(49))
    assert INITIALIZED + timedelta(hours=89) not in starts
    assert run.received_at == RECEIVED
    assert run.available_at is None
    assert not any(
        name.startswith("forecast-weather-index-") for name in list_documents(tmp_path, "solar")
    )


@pytest.mark.parametrize("part", ["pointer", "receipt", "manifest", "block"])
def test_retained_corruption_is_preserved_and_cannot_trigger_http_replacement(tmp_path, part):
    """A damaged active run fails closed before transport and retains the exact damaged original evidence."""
    store = make_store(tmp_path, SolarStore)
    asyncio.run(
        OpenMeteoSolarSource(FrozenRunSession(), store).read_run(SITE, INITIALIZED, None, RECEIVED)
    )
    documents = snapshot(store, tmp_path)
    prefixes = {
        "pointer": "forecast-weather-index-",
        "receipt": "forecast-weather-receipt-",
        "manifest": "forecast-weather-data-",
        "block": "block-",
    }
    target = next(name for name in documents if name.startswith(prefixes[part]))
    damaged = (
        "invalid-reference" if part == "pointer" else {**documents[target], "unexpected": True}
    )
    store.documents.write(target, damaged)
    before = snapshot(store, tmp_path)
    session = FrozenRunSession()
    with pytest.raises(ValueError):
        asyncio.run(
            OpenMeteoSolarSource(session, make_store(tmp_path, SolarStore)).read_run(
                SITE, INITIALIZED, None, RECEIVED + timedelta(days=1)
            )
        )
    assert session.calls == []
    assert snapshot(store, tmp_path) == before


def test_repeated_receipt_keeps_the_first_clock_and_changed_frozen_values_are_rejected(tmp_path):
    """Identical redownloads reuse the first receipt while changed provider values cannot replace a frozen run."""
    store = make_store(tmp_path, SolarStore)
    run = asyncio.run(
        OpenMeteoSolarSource(FrozenRunSession(), store).read_run(SITE, INITIALIZED, None, RECEIVED)
    )
    cache, query = ForecastWeatherCache(store), forecast_weather_query(SITE, INITIALIZED)
    later = RECEIVED + timedelta(days=1)
    repeated = replace(
        run, received_at=later, hours=tuple(replace(row, received_at=later) for row in run.hours)
    )
    before = snapshot(store, tmp_path)
    assert cache.write(query, repeated) == run
    changed = replace(run, hours=(replace(run.hours[0], ghi=600), *run.hours[1:]))
    with pytest.raises(ValueError, match="Frozen solar forecast changed"):
        cache.write(query, changed)
    assert snapshot(store, tmp_path) == before


def test_typed_run_validates_both_simulation_lags_and_actual_publication_constraints():
    """Required boundary coverage is independent of simulation origin and publication must remain truthful."""
    run = asyncio.run(
        OpenMeteoSolarSource(FrozenRunSession()).read_run(SITE, INITIALIZED, INITIALIZED, RECEIVED)
    )
    assert isinstance(run, ForecastWeatherRun)
    assert replace(run, forecast_origin=INITIALIZED + timedelta(hours=24)).hours == run.hours
    required = tuple(row for row in run.hours if row.start <= INITIALIZED + timedelta(hours=48))
    assert len(required) == 49
    assert replace(run, hours=required).forecast_origin == INITIALIZED
    with pytest.raises(ValueError, match="Incomplete frozen solar forecast horizon"):
        replace(run, hours=required[:-1])
    with pytest.raises(ValueError, match="Invalid frozen solar forecast identity"):
        replace(run, available_at=RECEIVED + timedelta(seconds=1))
    with pytest.raises(ValueError, match="Invalid frozen solar forecast identity"):
        replace(run, initialized_at=INITIALIZED + timedelta(hours=1))
    with pytest.raises(ValueError):
        replace(run, initialized_at=INITIALIZED.replace(tzinfo=None))


class LatestRunSession(FrozenRunSession):
    """Separate truthful latest-run metadata from exact original-run payloads."""

    def __init__(self, *, metadata=None, damage=None):
        """Supply independent publication evidence and optional original-run transport failure."""
        super().__init__(damage)
        self.metadata = metadata or {
            "last_run_initialisation_time": INITIALIZED.timestamp(),
            "last_run_availability_time": (INITIALIZED + timedelta(hours=6)).timestamp(),
        }
        self.metadata_failure = False

    def get(self, url, **kwargs):
        """Serve metadata without permitting live best-match or observed/reanalysis endpoints."""
        if url == "https://api.open-meteo.com/data/ecmwf_ifs/static/meta.json":
            self.calls.append((url, kwargs))
            if self.metadata_failure:
                raise OSError("Synthetic latest-run metadata outage")
            return Response(json.dumps(self.metadata).encode())
        assert url == "https://single-runs-api.open-meteo.com/v1/forecast"
        return super().get(url, **kwargs)


def test_current_forecast_uses_actual_latest_publication_without_aging_or_backdating(tmp_path):
    """Current forecasts use the exact published run at the current hour and retain cached original receipt times."""
    store = make_store(tmp_path, SolarStore)
    session = LatestRunSession()
    source = OpenMeteoSolarSource(session, store)
    now = INITIALIZED + timedelta(hours=8, minutes=15)
    first = asyncio.run(source.read(SITE, now))
    assert source.current_run.forecast_origin == INITIALIZED + timedelta(hours=8)
    assert source.current_run.initialized_at == INITIALIZED
    assert source.current_run.available_at is None
    assert source.service["available_at"] == (INITIALIZED + timedelta(hours=6)).isoformat()
    assert source.service["selection"] == "latest_available"
    assert all(row.received_at == now for row in first)
    later = now + timedelta(hours=4)
    second = asyncio.run(source.read(SITE, later))
    assert second == first
    assert source.current_run.forecast_origin == INITIALIZED + timedelta(hours=12)
    assert source.service["received_at"] == now.isoformat()
    assert len(session.calls) == 3
    assert session.calls[1][1]["params"]["run"] == "2026-04-01T12:00"
    assert source.forecast_runs[0].forecast_origin == INITIALIZED + timedelta(hours=24)
    session.metadata_failure = True
    with pytest.raises(OSError):
        asyncio.run(source.read(SITE, later))
    assert source.current_run.received_at == now
    assert len(session.calls) == 4


@pytest.mark.parametrize(
    "metadata",
    [
        {"last_run_initialisation_time": float("nan"), "last_run_availability_time": 0},
        {"last_run_initialisation_time": True, "last_run_availability_time": 0},
        {"last_run_initialisation_time": INITIALIZED.timestamp()},
        {
            "last_run_initialisation_time": INITIALIZED.timestamp(),
            "last_run_availability_time": (INITIALIZED + timedelta(days=1)).timestamp(),
        },
        {
            "last_run_initialisation_time": (INITIALIZED - timedelta(days=2)).timestamp(),
            "last_run_availability_time": (INITIALIZED - timedelta(days=1)).timestamp(),
        },
        {
            "last_run_initialisation_time": (INITIALIZED + timedelta(hours=1)).timestamp(),
            "last_run_availability_time": (INITIALIZED + timedelta(hours=6)).timestamp(),
        },
    ],
)
def test_unready_stale_or_invalid_latest_metadata_cannot_fall_back_to_other_weather(metadata):
    """Only finite, published, current and real-cycle initialization metadata may choose a live run."""
    session = LatestRunSession(metadata=metadata)
    with pytest.raises(ValueError):
        asyncio.run(OpenMeteoSolarSource(session).read(SITE, INITIALIZED + timedelta(hours=8)))
    assert len(session.calls) == 1


def test_current_site_change_and_restart_use_only_the_matching_frozen_cache(tmp_path):
    """Reopening the same site works without original-run HTTP while another site obtains independent evidence."""
    store = make_store(tmp_path, SolarStore)
    now = INITIALIZED + timedelta(hours=8)
    first = OpenMeteoSolarSource(LatestRunSession(), store)
    asyncio.run(first.read(SITE, now))
    session = LatestRunSession(damage="failure")
    restarted = OpenMeteoSolarSource(session, make_store(tmp_path, SolarStore))
    assert asyncio.run(restarted.read(SITE, now + timedelta(hours=1)))
    assert len(session.calls) == 1
    other = replace(SITE, latitude=SITE.latitude + 1)
    session.damage = None
    asyncio.run(restarted.read(other, now + timedelta(hours=1)))
    assert len(session.calls) == 3
    assert restarted.current_run.location_version != first.current_run.location_version
    assert len(restarted.forecast_runs) == 1
    assert restarted.forecast_runs[0].location_version == restarted.current_run.location_version


def test_historical_import_is_batched_newest_first_and_restarts_without_repeated_http(tmp_path):
    """Four daily noon runs advance each batch and a restart decodes each retained run once without HTTP."""
    now = INITIALIZED + timedelta(hours=8)
    store = make_store(tmp_path, SolarStore)
    session = FrozenRunSession()
    source = OpenMeteoSolarSource(
        session, store, history_start=lambda: INITIALIZED - timedelta(days=30)
    )
    runs = asyncio.run(source.import_runs(SITE, now))
    assert len(runs) == 4
    assert [options["params"]["run"] for _, options in session.calls] == [
        "2026-03-29T12:00",
        "2026-03-28T12:00",
        "2026-03-27T12:00",
        "2026-03-26T12:00",
    ]
    assert all(run.received_at == now and run.available_at is None for run in runs)
    restarted_session = FrozenRunSession("failure")
    restarted = OpenMeteoSolarSource(
        restarted_session,
        make_store(tmp_path, SolarStore),
        history_start=lambda: INITIALIZED - timedelta(days=30),
    )
    assert asyncio.run(restarted.import_runs(SITE, now + timedelta(hours=1))) == runs
    assert restarted_session.calls == []
    with pytest.raises(OSError):
        asyncio.run(restarted.import_runs(SITE, now + timedelta(hours=1)))
    assert len(restarted_session.calls) == 1
    assert asyncio.run(restarted.import_runs(SITE, now + timedelta(hours=1, minutes=14))) == runs
    assert len(restarted_session.calls) == 1
    with pytest.raises(OSError):
        asyncio.run(restarted.import_runs(SITE, now + timedelta(hours=1, minutes=15)))
    assert len(restarted_session.calls) == 2


def test_slow_historical_provider_does_not_block_current_forecast():
    """An independently waiting history request cannot delay current latest-run acquisition or task cancellation."""

    async def scenario():
        """Hold one old original run while proving the current weather path completes independently."""
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowHistorySource(OpenMeteoSolarSource):
            """Pause only old initialization requests without changing current provider semantics."""

            async def read_run(self, location, run_init, origin, now):
                """Keep the historical read pending until cancellation while current IFS remains available."""
                if run_init < INITIALIZED:
                    entered.set()
                    await release.wait()
                return await super().read_run(location, run_init, origin, now)

        source = SlowHistorySource(
            LatestRunSession(), history_start=lambda: INITIALIZED - timedelta(days=30)
        )
        task = asyncio.create_task(source.import_runs(SITE, INITIALIZED + timedelta(hours=8)))
        await entered.wait()
        weather = await asyncio.wait_for(
            source.read(SITE, INITIALIZED + timedelta(hours=8)), timeout=1
        )
        assert len(weather) == 95
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_new_background_runs_do_not_discard_usable_jobs_and_remain_coalesced(tmp_path, monkeypatch):
    """A worker publishes its captured valid result when only new ordinary training evidence arrives."""
    from open_energy_planner.solar.runtime import SolarRuntime
    from tests.app.solar.support import plant

    now = INITIALIZED + timedelta(hours=8)
    source = OpenMeteoSolarSource(LatestRunSession())
    weather = asyncio.run(source.read(SITE, now))
    store = make_store(tmp_path, SolarStore)
    store.save(plant())
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, weather, service=source.service)
    captured = []

    def candidates(*arguments):
        """Record exactly the immutable forecast-run snapshot passed to the learning worker."""
        captured.append(arguments[-1])
        return {}

    monkeypatch.setattr(runtime, "_learned_candidates", candidates)
    queued = runtime.job(now)
    result = runtime.build(queued)
    assert captured == [()]
    assert runtime.set_forecast_runs(SITE, source.forecast_runs)
    assert runtime.install(result, now)
    assert runtime.last_key == queued[0]
    assert runtime.job(now + timedelta(seconds=30)) is None
    following = runtime.job(now + timedelta(seconds=60))
    assert following[7] == source.forecast_runs
    assert following[0] != queued[0]
    assert runtime.document(now)["weather_service"] == source.service
    assert runtime.job(now + timedelta(hours=4)) is not None
    assert runtime.job(INITIALIZED + timedelta(hours=25)) is None
    assert runtime.issue == "solar_weather_expired"
    runtime.configure(replace(plant(), sources=(replace(plant().sources[0], tilt=45),)))
    assert not runtime.install(result, now)
    runtime.set_weather(replace(SITE, longitude=SITE.longitude + 1), weather)
    assert runtime.forecast_runs == ()
    assert not runtime.install(result, now)


def test_http_lifecycle_publishes_current_forecast_while_history_waits_and_cancels_cleanly(
    tmp_path, monkeypatch
):
    """Real app lifecycle owns separate history/current tasks and drains the waiting history task at shutdown."""
    from open_energy_planner.solar.runtime import SolarRuntime
    from open_energy_planner.solar.wire import configuration_document
    from tests.app.forecast.support.http import configure_forecasts, running_forecasts
    from tests.app.solar.support import plant, weather

    monkeypatch.setattr("open_energy_planner.solar.api.SOLAR_WORKER_POLL_SECONDS", 0.01)
    monkeypatch.setattr(SolarRuntime, "_learned_candidates", lambda *_arguments: {})

    async def scenario():
        """Publish a usable forecast through authenticated HTTP while an independent history request stays pending."""
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class WaitingHistorySource(OpenMeteoSolarSource):
            """Expose the actual current/history source protocol with an intentionally slow history provider."""

            async def read(self, location, now):
                """Supply complete current physical weather and explicit synthetic publication provenance."""
                initialized = now.replace(
                    hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0
                ) - timedelta(hours=6)
                self.service = {
                    "model": "ecmwf_ifs",
                    "selection": "latest_available",
                    "initialized_at": initialized.isoformat(),
                    "received_at": now.isoformat(),
                    "available_at": (initialized + timedelta(hours=1)).isoformat(),
                    "policy": "synthetic-current-ifs",
                }
                return weather(now, days=4)

            async def import_runs(self, location, now):
                """Wait until application shutdown to verify independent task cancellation and cleanup."""
                entered.set()
                try:
                    await release.wait()
                finally:
                    cancelled.set()
                return ()

        source = WaitingHistorySource(None)

        @asynccontextmanager
        async def factory():
            """Provide a controlled production-source protocol without external requests or credentials."""
            yield source

        async with running_forecasts(tmp_path, weather_factory=factory) as (session, url):
            definition = configuration_document(plant())
            definition.pop("version")
            await configure_forecasts(session, url, solar_definition=definition)
            await asyncio.wait_for(entered.wait(), timeout=2)
            for _ in range(200):
                async with session.get(url + "/v1/solar") as response:
                    document = await response.json()
                if document["forecast"]:
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError(document)
            assert document["weather_service"]["model"] == "ecmwf_ifs"
            assert not cancelled.is_set()
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_a_known_missing_run_does_not_block_older_training_and_is_retried_independently():
    """A 404 leaves an explicit run gap while older days advance and that exact initialization retries later."""

    class MissingRunSession(FrozenRunSession):
        """Keep one original initialization absent independently of all older daily forecast runs."""

        def __init__(self):
            """Initially fail the newest historical noon initialization only."""
            super().__init__()
            self.missing = True

        def get(self, url, **kwargs):
            """Represent the provider's documented missing-run 404 without a broad outage."""
            if kwargs["params"]["run"] == "2026-03-29T12:00" and self.missing:
                self.calls.append((url, kwargs))
                raise ClientResponseError(None, (), status=404)
            return super().get(url, **kwargs)

    now = INITIALIZED + timedelta(hours=8)
    session = MissingRunSession()
    source = OpenMeteoSolarSource(session, history_start=lambda: INITIALIZED - timedelta(days=30))
    first = asyncio.run(source.import_runs(SITE, now))
    missing = datetime(2026, 3, 29, 12, tzinfo=UTC)
    assert len(first) == 3
    assert source.history_issues == {missing: "forecast_run_missing"}
    assert source.history_retry_after is None
    second = asyncio.run(source.import_runs(SITE, now + timedelta(minutes=5)))
    assert len(second) == 7
    assert sum(options["params"]["run"] == "2026-03-29T12:00" for _, options in session.calls) == 1
    assert len(asyncio.run(source.import_runs(SITE, now + timedelta(minutes=15)))) == 10
    assert sum(options["params"]["run"] == "2026-03-29T12:00" for _, options in session.calls) == 2
    session.missing = False
    recovered = asyncio.run(source.import_runs(SITE, now + timedelta(minutes=45)))
    assert any(run.initialized_at == missing for run in recovered)
    assert source.history_issues == {}


def test_provider_5xx_stops_after_one_attempt_and_retains_global_backoff():
    """A broad provider failure cannot multiply its timeout or error by a batch or years of history."""

    class FailingSession(FrozenRunSession):
        """Represent a provider-wide original forecast outage."""

        def get(self, url, **kwargs):
            """Count the single bounded attempt and reject the provider request."""
            self.calls.append((url, kwargs))
            raise ClientResponseError(None, (), status=503)

    now = INITIALIZED + timedelta(hours=8)
    session = FailingSession()
    source = OpenMeteoSolarSource(session, history_start=lambda: INITIALIZED - timedelta(days=30))
    with pytest.raises(ClientResponseError):
        asyncio.run(source.import_runs(SITE, now))
    assert len(session.calls) == 1
    assert asyncio.run(source.import_runs(SITE, now + timedelta(minutes=14))) == ()
    assert len(session.calls) == 1


def test_cancellation_drains_the_frozen_cache_transaction_before_cleanup(tmp_path, monkeypatch):
    """Cancelled import cleanup waits for the actual database writer and leaves one fully committed original receipt."""
    entered, release = ThreadEvent(), ThreadEvent()
    original = ForecastWeatherCache.write

    def waiting_write(cache, query, run):
        """Hold one real pointer transaction until the cancellation drain can be observed."""
        with cache.store.transaction():
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("Synthetic database writer was not released")
            return original(cache, query, run)

    monkeypatch.setattr(ForecastWeatherCache, "write", waiting_write)
    store = make_store(tmp_path, SolarStore)

    async def scenario():
        """Cancel only after the writer starts and prove its complete commit precedes task termination."""
        task = asyncio.create_task(
            OpenMeteoSolarSource(FrozenRunSession(), store).read_run(
                SITE, INITIALIZED, None, RECEIVED
            )
        )
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(
            asyncio.to_thread(
                store.archive, "independent-source", "after-cancel", {"usable": True}
            ),
            timeout=5,
        )
        assert store.documents.read("independent-source-after-cancel.json") == {"usable": True}

    asyncio.run(scenario())
    saved = ForecastWeatherCache(make_store(tmp_path, SolarStore)).read(
        forecast_weather_query(SITE, INITIALIZED)
    )
    assert saved.received_at == RECEIVED
    assert len(saved.hours) == 95


def test_captured_weather_service_is_archived_with_old_forecast_rows_despite_a_live_update(
    tmp_path, monkeypatch
):
    """Immutable baseline and candidate provenance uses the captured old service even when live weather changes mid-worker."""
    from open_energy_planner.solar.calculation.predictor import PVWattsPredictor
    from open_energy_planner.solar.runtime import SolarRuntime
    from tests.app.solar.support import plant

    now = INITIALIZED + timedelta(hours=8)
    source = OpenMeteoSolarSource(LatestRunSession())
    original_weather = asyncio.run(source.read(SITE, now))
    original_service = dict(source.service)
    store = make_store(tmp_path, SolarStore)
    store.save(plant())
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, original_weather, service=original_service)
    monkeypatch.setattr(runtime, "_learned_candidates", lambda *_arguments: {})

    def candidate(configuration, location, weather, issued_at, _models):
        """Supply a distinct deterministic candidate only to exercise its independent archive projection."""
        return replace(
            PVWattsPredictor().forecast(configuration, location, weather, issued_at),
            model="solar-xgboost-production-v2",
            quality="shadow",
        )

    monkeypatch.setattr("open_energy_planner.solar.runtime.learned_forecast", candidate)
    queued = runtime.job(now)
    assert queued[8] == original_service
    assert queued[8] is not runtime.weather_service
    next_initialized = INITIALIZED + timedelta(hours=6)
    later_source = OpenMeteoSolarSource(
        LatestRunSession(
            metadata={
                "last_run_initialisation_time": next_initialized.timestamp(),
                "last_run_availability_time": (next_initialized + timedelta(hours=1)).timestamp(),
            }
        )
    )
    later_weather = asyncio.run(later_source.read(SITE, now + timedelta(hours=6)))
    runtime.set_weather(SITE, later_weather, service=later_source.service)
    assert queued[8] == original_service
    result = runtime.build(queued)
    baseline, independent = result[4], result[3]
    assert (
        baseline.weather_version
        != PVWattsPredictor()
        .forecast(plant(), SITE, later_weather, now + timedelta(hours=6))
        .weather_version
    )
    for forecast in (baseline, independent):
        archived = store.documents.read("forecast-" + forecast.version + ".json")
        assert archived["weather_service"] == original_service
        assert archived["forecast_weather_source"] == {
            "model": "ecmwf_ifs",
            "initialized_at": "2026-04-01T12:00:00+00:00",
            "location_version": source.current_run.location_version,
            "weather_version": forecast.weather_version,
            "policy": source.current_run.policy_version,
        }
    context = store.documents.read("quality-candidates-" + result[1].version + ".json")
    assert context["weather_service"] == original_service
    manifest = store.documents.read("weather-" + baseline.weather_version + ".json")
    first = manifest["blocks"][0]
    values = store.documents.read("block-" + first["block"] + ".json")
    assert datetime.fromisoformat(values["hours"][0]["start"]) == INITIALIZED
    assert len(first["receipts"]) == 1
    assert first["receipts"][0][0] == 0
    assert datetime.fromisoformat(first["receipts"][0][1]) == now
    assert not runtime.install(result, now + timedelta(hours=6))


@pytest.mark.parametrize("minute", [0, 15, 30, 45])
def test_live_49_hour_guard_covers_the_final_partial_planning_hour(tmp_path, monkeypatch, minute):
    """Exactly 49 normalized hours cover every quarter-origin horizon and expiry rejects the next missing boundary."""
    from open_energy_planner.solar.runtime import SolarRuntime
    from tests.app.solar.support import plant

    received = INITIALIZED + timedelta(hours=8)
    source = OpenMeteoSolarSource(LatestRunSession())
    rows = asyncio.run(source.read(SITE, received))
    selected = tuple(row for row in rows if received <= row.start <= received + timedelta(hours=48))
    assert len(selected) == 49
    store = make_store(tmp_path, SolarStore)
    store.save(plant())
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, selected, service=source.service)
    monkeypatch.setattr(runtime, "_learned_candidates", lambda *_arguments: {})
    now = received + timedelta(minutes=minute)
    queued = runtime.job(now)
    assert queued is not None
    forecast = runtime.build(queued)[4]
    assert forecast.energies[0].start == now
    last = forecast.energies[-1]
    assert last.start + timedelta(hours=last.duration_h) == now + timedelta(hours=48)
    assert runtime.job(received + timedelta(hours=1)) is None
    assert runtime.issue == "solar_weather_expired"


def test_history_outage_backoff_does_not_block_cached_other_sites_or_current_weather(tmp_path):
    """A historical 503 leaves direct cached site/day reads and healthy latest current selection available."""
    now = INITIALIZED + timedelta(hours=8)
    other_site, other_init = (
        replace(SITE, longitude=SITE.longitude + 1),
        INITIALIZED - timedelta(days=4),
    )
    store = make_store(tmp_path, SolarStore)
    seed = OpenMeteoSolarSource(FrozenRunSession(), store)
    cached_current = asyncio.run(
        seed.read_run(SITE, INITIALIZED, INITIALIZED + timedelta(hours=8), now)
    )
    cached_other = asyncio.run(seed.read_run(other_site, other_init, None, now))

    class HistoryOutageSession(LatestRunSession):
        """Fail only the uncached old initialization while preserving truthful current-run metadata."""

        def get(self, url, **kwargs):
            """Keep the remote history failure distinct from current service and cached query identities."""
            if kwargs.get("params", {}).get("run") == "2026-03-29T12:00":
                self.calls.append((url, kwargs))
                raise ClientResponseError(None, (), status=503)
            return super().get(url, **kwargs)

    session = HistoryOutageSession()
    source = OpenMeteoSolarSource(
        session, store, history_start=lambda: INITIALIZED - timedelta(days=30)
    )
    with pytest.raises(ClientResponseError):
        asyncio.run(source.import_runs(SITE, now))
    assert len(session.calls) == 1
    assert asyncio.run(source.read_run(other_site, other_init, None, now)) == cached_other
    assert len(session.calls) == 1
    assert asyncio.run(source.read(SITE, now)) == cached_current.hours
    assert len(session.calls) == 2
    assert source.history_retry_after == now + timedelta(minutes=15)


def test_one_incomplete_original_run_cannot_block_older_training_or_current_forecasts(tmp_path):
    """A valid 49-hour window with a later gap remains usable but cannot cache as complete or stall older runs."""

    class IncompleteRunSession(LatestRunSession):
        """Damage only one old initialization outside its required simulation horizon."""

        def __init__(self):
            """Initially leave one optional late hour missing from the newest historical run."""
            super().__init__()
            self.incomplete = True

        def get(self, url, **kwargs):
            """Keep current and older runs complete while the selected historical run has one gap."""
            if kwargs.get("params", {}).get("run") == "2026-03-29T12:00" and self.incomplete:
                self.damage = "late_gap"
            try:
                return super().get(url, **kwargs)
            finally:
                self.damage = None

    now = INITIALIZED + timedelta(hours=8)
    missing = datetime(2026, 3, 29, 12, tzinfo=UTC)
    store = make_store(tmp_path, SolarStore)
    session = IncompleteRunSession()
    source = OpenMeteoSolarSource(
        session, store, history_start=lambda: INITIALIZED - timedelta(days=30)
    )
    first = asyncio.run(source.import_runs(SITE, now))
    assert len(first) == 3
    assert source.history_issues == {missing: "forecast_run_unusable"}
    assert source.history_retry_after is None
    query = forecast_weather_query(SITE, missing)
    assert ForecastWeatherCache(store).read(query) is None
    current = asyncio.run(source.read(SITE, now))
    assert len(current) == 95
    assert source.service["selection"] == "latest_available"
    second = asyncio.run(source.import_runs(SITE, now + timedelta(minutes=5)))
    assert len(second) == 8
    assert (
        sum(
            options.get("params", {}).get("run") == "2026-03-29T12:00"
            for _, options in session.calls
        )
        == 1
    )
    asyncio.run(source.import_runs(SITE, now + timedelta(minutes=15)))
    assert (
        sum(
            options.get("params", {}).get("run") == "2026-03-29T12:00"
            for _, options in session.calls
        )
        == 2
    )
    assert ForecastWeatherCache(store).read(query) is None
    session.incomplete = False
    recovered = asyncio.run(source.import_runs(SITE, now + timedelta(minutes=45)))
    assert any(run.initialized_at == missing for run in recovered)
    assert source.history_issues == {}
    assert len(ForecastWeatherCache(store).read(query).hours) == 95


def test_frozen_run_version_is_computed_once_without_changing_its_original_digest(monkeypatch):
    """Repeated source lookup serializes one immutable run once and retains its independently computed original identity."""
    run = asyncio.run(
        OpenMeteoSolarSource(FrozenRunSession()).read_run(SITE, INITIALIZED, None, RECEIVED)
    )
    original = asdict(run)
    expected = hashlib.sha256(
        json.dumps(original, default=str, sort_keys=True).encode()
    ).hexdigest()
    serialized = []

    def serialize(value):
        """Count actual recursive serialization while retaining the standard dataclass conversion."""
        serialized.append(value)
        return asdict(value)

    monkeypatch.setattr("open_energy_planner.solar.forecast_weather.asdict", serialize)
    assert run.version == expected
    assert run.version == expected
    assert run.version == expected
    assert len(serialized) == 1
    assert serialized[0] is run
    assert asdict(run) == original
    with pytest.raises(FrozenInstanceError):
        run.received_at = RECEIVED + timedelta(hours=1)


def test_replacing_frozen_run_clocks_or_values_computes_each_new_identity_once(monkeypatch):
    """New origins, real receipt clocks and measured weather values receive independent caches without stale copied digests."""
    run = asyncio.run(
        OpenMeteoSolarSource(FrozenRunSession()).read_run(SITE, INITIALIZED, None, RECEIVED)
    )
    original_version = run.version
    later = RECEIVED + timedelta(hours=1)
    replacements = (
        replace(run, forecast_origin=INITIALIZED),
        replace(
            run,
            received_at=later,
            hours=tuple(replace(row, received_at=later) for row in run.hours),
        ),
        replace(run, hours=(replace(run.hours[0], ghi=600), *run.hours[1:])),
    )
    expected = tuple(
        hashlib.sha256(json.dumps(asdict(value), default=str, sort_keys=True).encode()).hexdigest()
        for value in replacements
    )
    serialized = []

    def serialize(value):
        """Execute the original serialization and count only version work on the replaced instances."""
        serialized.append(value)
        return asdict(value)

    monkeypatch.setattr("open_energy_planner.solar.forecast_weather.asdict", serialize)
    for value, identity in zip(replacements, expected, strict=True):
        assert value.version == identity
        assert value.version == identity
        assert identity != original_version
    assert len(serialized) == 3
    assert all(value is copied for value, copied in zip(serialized, replacements, strict=True))
    assert run.version == original_version
    assert len(serialized) == 3


def test_memoized_run_version_preserves_frozen_cache_round_trip_and_original_archive(tmp_path):
    """Populating the in-memory digest cache cannot add durable fields, alter original receipts or change reopened identities."""
    store = make_store(tmp_path, SolarStore)
    run = asyncio.run(
        OpenMeteoSolarSource(FrozenRunSession(), store).read_run(SITE, INITIALIZED, None, RECEIVED)
    )
    original = asdict(run)
    expected = hashlib.sha256(
        json.dumps(original, default=str, sort_keys=True).encode()
    ).hexdigest()
    saved = snapshot(store, tmp_path)
    assert run.version == expected
    assert asdict(run) == original
    reopened = ForecastWeatherCache(make_store(tmp_path, SolarStore)).read(
        forecast_weather_query(SITE, INITIALIZED)
    )
    assert reopened == run
    assert reopened.version == expected
    assert asdict(reopened) == original
    assert snapshot(store, tmp_path) == saved


@pytest.mark.parametrize("unusable", [False, True])
def test_missing_run_retries_grow_to_one_day_without_stopping_other_originals(unusable):
    """Repeated gaps wait 15, 30, 60 minutes up to one day, while other runs progress independently."""
    missing = datetime(2026, 3, 29, 12, tzinfo=UTC)

    class MissingSession(FrozenRunSession):
        """Keep one known initialization missing or incomplete while serving every other request."""

        def __init__(self):
            """Start with an unresolved gap and allow an actual later provider recovery."""
            super().__init__()
            self.missing = True

        def get(self, url, **kwargs):
            """Return the missing run's failure independently of older and newer complete runs."""
            failed = self.missing and kwargs["params"]["run"] == "2026-03-29T12:00"
            if failed and not unusable:
                self.calls.append((url, kwargs))
                raise ClientResponseError(None, (), status=404)
            self.damage = "required_gap" if failed else None
            return super().get(url, **kwargs)

    session = MissingSession()
    source = OpenMeteoSolarSource(session, history_start=lambda: missing - timedelta(days=5))
    instant = INITIALIZED + timedelta(hours=8)
    for attempts, minutes in enumerate((15, 30, 60, 120, 240, 480, 960, 1440, 1440, 1440), start=1):
        asyncio.run(source.import_runs(SITE, instant))
        assert (
            sum(options["params"]["run"] == "2026-03-29T12:00" for _, options in session.calls)
            == attempts
        )
        deadline = instant + timedelta(minutes=minutes)
        assert source._missing_run_retries[missing] == deadline
        assert source.history_retry_after is None
        assert len(source.forecast_runs) > 0
        asyncio.run(source.import_runs(SITE, deadline - timedelta(seconds=1)))
        assert (
            sum(options["params"]["run"] == "2026-03-29T12:00" for _, options in session.calls)
            == attempts
        )
        instant = deadline
    assert source._missing_run_attempts[missing] == 8
    session.missing = False
    asyncio.run(source.import_runs(SITE, instant))
    assert any(run.initialized_at == missing for run in source.forecast_runs)
    assert missing not in source._missing_run_retries
    assert missing not in source._missing_run_attempts
    assert source.history_issues == {}
    source = OpenMeteoSolarSource(MissingSession(), history_start=lambda: missing)
    asyncio.run(source.import_runs(SITE, INITIALIZED + timedelta(hours=8)))
    assert source._missing_run_attempts[missing] == 1
    source._select_site(replace(SITE, longitude=SITE.longitude + 1))
    assert source._missing_run_retries == source._missing_run_attempts == {}


@pytest.mark.parametrize("failure", ["404", "503", "unusable", "transport"])
def test_old_site_import_failure_cannot_set_retry_state_for_a_new_site(failure):
    """A site change during an awaited run read keeps the new site's retry state clean."""
    changed = replace(SITE, longitude=SITE.longitude + 1)

    class SwitchingSource(OpenMeteoSolarSource):
        """Complete an old request with a controlled failure after installing the new site."""

        async def read_run(self, location, run_init, origin, now):
            """Represent a delayed provider/cache error without backdating the new site's state."""
            self._select_site(changed)
            if failure in ("404", "503"):
                raise ClientResponseError(None, (), status=int(failure))
            if failure == "unusable":
                raise ValueError("Synthetic old cache failure")
            raise OSError("Synthetic old transport failure")

    source = SwitchingSource(
        FrozenRunSession(), history_start=lambda: INITIALIZED - timedelta(days=5)
    )
    assert asyncio.run(source.import_runs(SITE, INITIALIZED + timedelta(hours=8))) == ()
    assert source._run_site == version(asdict(changed))
    assert (
        source._missing_run_attempts == source._missing_run_retries == source.history_issues == {}
    )
    assert source.history_retry_after is None
