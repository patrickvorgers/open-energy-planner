"""Use only earlier training and original temperature forecasts in device replays."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from aiohttp import web

from open_energy_planner.demand.api import register
from open_energy_planner.demand.calculation.forecasting import RidgeDemandModel
from open_energy_planner.demand.calculation.model import DemandConfiguration, DemandHour, EnergyTerm
from open_energy_planner.demand.storage import DemandStore
from open_energy_planner.demand.training import daily_models
from open_energy_planner.forecast_selection.device_history import (
    replay_days,
    replay_family,
    replay_scores,
    replay_season,
    replay_weather_history_floor,
    spread_replays,
)
from open_energy_planner.weather.model import TemperatureHour, WeatherHour
from open_energy_planner.weather.run import ForecastWeatherRun
from tests.database.support.archive import make_store


def test_replay_spreads_24_days_and_replaces_missing_candidates():
    """Cover both day types and several weekdays across all meteorological seasons."""
    first = date(2026, 1, 1)
    candidates = [(first + timedelta(days=index), None, None) for index in range(365)]
    rejected = set()

    def score(candidate):
        """Skip the first candidate in each season/day-type without consulting an error score."""
        day = candidate[0]
        group = replay_season(day), day.weekday() >= 5
        if group not in rejected:
            rejected.add(group)
            return None
        return {"day": day}

    selected = spread_replays(candidates, "UTC", score)
    assert len(selected) == 24 and len(rejected) == 8
    for season in ("winter", "spring", "summer", "autumn"):
        weekdays = {row["day"].weekday() for row in selected if replay_season(row["day"]) == season}
        assert len(weekdays - {5, 6}) == 4
        assert {5, 6} <= weekdays
    rejected.clear()
    assert spread_replays(reversed(candidates), "UTC", score) == selected


def test_replay_spreads_cold_and_mild_original_forecasts():
    """Use forecast temperatures to cover both regimes for weekdays and weekends."""
    first = datetime(2026, 1, 1, tzinfo=UTC)
    candidates = []
    for index in range(365):
        start = first + timedelta(days=index)
        origin = start - timedelta(days=1)
        temperature = 2 if index // 7 % 2 == 0 else 14
        run = SimpleNamespace(
            hours=tuple(
                WeatherHour(start + timedelta(hours=hour), 0, 0, 0, temperature, 0, origin)
                for hour in range(24)
            )
        )
        candidates.append((start.date(), origin, run))

    def score(candidate):
        day, _, run = candidate
        return {"day": day, "temperature": run.hours[0].celsius}

    selected = spread_replays(candidates, "UTC", score)
    for season in ("winter", "spring", "summer", "autumn"):
        for weekend in (False, True):
            assert {
                row["temperature"]
                for row in selected
                if replay_season(row["day"]) == season and (row["day"].weekday() >= 5) == weekend
            } == {2, 14}


def test_slow_background_replay_does_not_hold_live_prediction():
    """Finish two live requests while one historical batch is blocked, then install evidence."""

    async def verify():
        """Exercise the actual app lifecycle queue and off-loop writers."""
        started = Event()
        release = Event()
        installed = asyncio.Event()

        def build(job, replay_jobs):
            """Produce immediate forecast results and hand off a historical batch."""
            replay_jobs.append("background")
            return job[0]

        def evaluate(binding, jobs, timezone, stop):
            """Hold background testing until both live results have been delivered."""
            assert jobs == ["background"] and timezone == "UTC" and not stop.is_set()
            started.set()
            assert release.wait(5)
            return binding, {"completed": True}

        def install(binding, evidence):
            """Confirm only the current result is published to the next live build."""
            assert binding == ("current",) and evidence == {"completed": True}
            installed.set()

        demand = SimpleNamespace(
            build=build,
            evaluate_replays=evaluate,
            install_replays=install,
            replay_binding=lambda: ("current",),
        )
        app = web.Application()
        build_live = register(
            app,
            demand,
            live=None,
            setup_catalogue=None,
            lock=asyncio.Lock(),
            persist_then_install=None,
            snapshot=None,
        )
        lifecycle = app.cleanup_ctx[-1](app)
        await anext(lifecycle)
        try:
            assert (
                await asyncio.wait_for(
                    build_live((1, SimpleNamespace(timezone="UTC"), ("current",))), 3
                )
                == 1
            )
            assert await asyncio.to_thread(started.wait, 3)
            assert (
                await asyncio.wait_for(
                    build_live((2, SimpleNamespace(timezone="UTC"), ("current",))), 3
                )
                == 2
            )
            assert not installed.is_set()
            release.set()
            await asyncio.wait_for(installed.wait(), 3)
        finally:
            release.set()
            await lifecycle.aclose()

    asyncio.run(verify())


def test_replay_weather_history_floor_changes_only_at_utc_midnight():
    """Keep intraday polls from resetting the scheduled weather refresh."""
    midnight = datetime(2026, 8, 24, tzinfo=UTC)
    assert replay_weather_history_floor(midnight) == replay_weather_history_floor(
        midnight + timedelta(hours=23, minutes=59)
    )
    assert replay_weather_history_floor(midnight + timedelta(days=1)) == (
        replay_weather_history_floor(midnight) + timedelta(days=1)
    )


def test_replay_preserves_original_weather_and_cannot_fit_held_out_energy(
    tmp_path, monkeypatch, caplog
):
    """Temperature models win independent replay. Corrected held-out energy changes scores only."""
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    initialized = now - timedelta(days=4)
    origin = initialized + timedelta(days=1)
    run = ForecastWeatherRun(
        initialized_at=initialized,
        received_at=now,
        location_version="a" * 64,
        hours=tuple(
            WeatherHour(initialized + timedelta(hours=index), 0, 0, 0, 6, 0, now)
            for index in range(95)
        ),
    )
    ((day, captured_origin, evidence),) = replay_days(now, "UTC", (run,))
    assert captured_origin == origin
    assert evidence.version == run.version
    target = datetime.combine(day, datetime.min.time(), UTC)
    samples, historical_weather = [], []
    for index in range(90 * 24):
        hour = now - timedelta(hours=90 * 24 - index)
        temperature = 6 + 12 * ((index // 24) % 4) / 3
        energy = 1.4 if temperature == 6 else 0.1
        if target <= hour < target + timedelta(days=1):
            # Realized weather in this synthetic test would lead to the wrong forecast.
            temperature, energy = 18, 1.4
        samples.append(DemandHour(hour, energy, energy))
        historical_weather.append(TemperatureHour(hour, temperature, now))
    configuration = DemandConfiguration("UTC", 0, (EnergyTerm("sensor.device", 1),))
    models = daily_models(
        None, configuration, samples, now, temperatures=historical_weather, weather_sensitive=True
    )
    archive = make_store(tmp_path, DemandStore).documents

    def temporarily_unavailable(*args, **kwargs):
        raise RuntimeError("Training engine temporarily unavailable")

    with monkeypatch.context() as transient:
        transient.setattr(RidgeDemandModel, "fit", temporarily_unavailable)
        for _ in range(2):
            assert not replay_scores(
                archive,
                configuration,
                tuple(samples),
                historical_weather,
                ((day, origin, run),),
                models,
                (),
            )
    assert caplog.text.count("Device forecast history replay failed") == 1

    caplog.set_level("INFO", logger=replay_scores.__module__)
    (window,) = replay_scores(
        archive,
        configuration,
        tuple(samples),
        historical_weather,
        ((day, origin, run),),
        models,
        (),
    )
    assert "Device forecast history replay recovered" in caplog.text
    assert window["source"] == "historical_replay"
    assert window["hours"] == 24
    assert set(window["mae_kwh"]) == {"baseline", "ridge", "weather"}
    assert window["mae_kwh"]["weather"] < window["mae_kwh"]["baseline"]
    corrected = tuple(
        DemandHour(sample.start, 2.8, 2.8)
        if target <= sample.start < target + timedelta(days=1)
        else sample
        for sample in samples
    )
    archive.engine.dispose()
    restarted_archive = make_store(tmp_path, DemandStore).documents
    with monkeypatch.context() as cached:
        cached.setattr(RidgeDemandModel, "fit", temporarily_unavailable)
        cached.setattr(f"{replay_scores.__module__}.mean_absolute_error", temporarily_unavailable)
        assert replay_scores(
            restarted_archive,
            configuration,
            tuple(samples),
            historical_weather,
            ((day, origin, run),),
            models,
            (),
        ) == (window,)
    with monkeypatch.context() as cached:
        cached.setattr(RidgeDemandModel, "fit", temporarily_unavailable)
        (updated,) = replay_scores(
            restarted_archive,
            configuration,
            corrected,
            historical_weather,
            ((day, origin, run),),
            models,
            (),
        )
    assert updated["forecast_version"] == window["forecast_version"]
    assert updated["actual_version"] != window["actual_version"]
    assert not replay_scores(
        restarted_archive,
        configuration,
        corrected,
        historical_weather,
        ((day, origin, run),),
        models,
        (day.isoformat(),),
    )


def test_replay_replaces_incomplete_measured_day_with_complete_weekend(tmp_path):
    """Use actual measured-hour admission when the newest Saturday has a gap."""
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    complete = now.date() - timedelta(days=9)
    incomplete = complete + timedelta(days=7)
    configuration = DemandConfiguration("UTC", 0, (EnergyTerm("sensor.device", 1),))
    windows = tuple(
        (day, datetime.combine(day, datetime.min.time(), UTC) - timedelta(hours=12), None)
        for day in (complete, incomplete)
    )
    missing = datetime.combine(incomplete, datetime.min.time(), UTC) + timedelta(hours=12)
    samples = tuple(
        DemandHour(hour, 0.4, 0.4)
        for index in range(60 * 24)
        if (hour := now - timedelta(hours=60 * 24 - index)) != missing
    )
    models = daily_models(None, configuration, samples, now, weather_sensitive=False)
    archive = make_store(tmp_path, DemandStore).documents
    (result,) = replay_scores(archive, configuration, samples, (), windows, models, ())
    assert result["day"] == complete.isoformat() and result["hours"] == 24


def test_replay_persists_completed_day_before_stopping_and_checks_archive_on_resume(tmp_path):
    """Keep a finished score across cancellation and reject corrupted retained predictions."""
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    first = now.date() - timedelta(days=9)
    second = now.date() - timedelta(days=8)
    configuration = DemandConfiguration("UTC", 0, (EnergyTerm("sensor.device", 1),))
    windows = tuple(
        (day, datetime.combine(day, datetime.min.time(), UTC) - timedelta(hours=12), None)
        for day in (first, second)
    )
    samples = tuple(
        DemandHour(now - timedelta(hours=hour), 0.4, 0.4) for hour in range(60 * 24, 0, -1)
    )
    models = daily_models(None, configuration, samples, now, weather_sensitive=False)
    archive = make_store(tmp_path, DemandStore).documents
    path = f"device-replay-{replay_family(configuration, models)}.json"

    def after_first_commit():
        """Stop only after the archive stores the completed selection."""
        try:
            return len(archive.read(path)["replays"]) == 1
        except FileNotFoundError:
            return False

    (completed,) = replay_scores(
        archive, configuration, samples, (), windows, models, (), stopping=after_first_commit
    )
    assert archive.read(path)["replays"][completed["day"]]["score"] == completed

    resumed = replay_scores(archive, configuration, samples, (), windows, models, ())
    assert [result["day"] for result in resumed] == [first.isoformat(), second.isoformat()]
    assert completed in resumed

    envelope = archive.read(path)
    envelope["replays"][completed["day"]]["origin"] = "changed without a new checksum"
    archive.write(path, envelope)
    with pytest.raises(ValueError, match="^Invalid archived device replay checksum or coverage$"):
        replay_scores(archive, configuration, samples, (), windows, models, ())
