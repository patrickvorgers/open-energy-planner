"""Verify solar-owned availability, conditional recovery and shared planning admission."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.home_assistant.observations import InputsUnavailable
from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.notifications.collection import NotificationCenter
from open_energy_planner.notifications.model import NoticeText, Notification
from open_energy_planner.runtime.diagnostics.planning_notifications import (
    PlanningStatus,
    explain_planning_problem,
    planning_notifications,
)
from open_energy_planner.solar.basis import forecast_usable
from open_energy_planner.solar.notifications import (
    explain_solar_problem,
    solar_forecast_notifications,
)
from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.solar.storage import SolarStore
from tests.app.forecast.support.basis import forecast_basis
from tests.app.forecast.support.http import configure_forecasts, running_forecasts
from tests.app.solar.test_solar import SITE, START, plant, weather
from tests.database.support.archive import make_store


@pytest.fixture
def solar_runtime(tmp_path):
    """Build a real physical forecast from complete independent weather in an isolated store."""
    runtime = SolarRuntime(make_store(tmp_path, SolarStore))
    runtime.configure(plant())
    runtime.set_weather(SITE, weather())
    assert runtime.install(runtime.build(runtime.job(START)), START)
    return runtime


@pytest.mark.parametrize(
    ("condition", "usable"),
    [
        ("current", True),
        ("before_expiry", True),
        ("expiry", False),
        ("future", False),
        ("missing", False),
        ("configuration", False),
        ("location", False),
        ("no_pv", True),
    ],
)
def test_solar_notifications_and_planning_use_identical_admission(solar_runtime, condition, usable):
    """Only current, matching forecasts or an explicitly PV-free installation satisfy solar admission."""
    runtime, now = solar_runtime, START
    if condition == "before_expiry":
        now += timedelta(minutes=30, microseconds=-1)
    elif condition == "expiry":
        now += timedelta(minutes=30)
    elif condition == "future":
        runtime.forecast = replace(runtime.forecast, issued_at=START + timedelta(microseconds=1))
    elif condition == "missing":
        runtime.forecast = None
    elif condition == "configuration":
        runtime.forecast = replace(runtime.forecast, configuration_version="different")
    elif condition == "location":
        runtime.forecast = replace(
            runtime.forecast, location=SiteLocation(53, 6, "Europe/Amsterdam")
        )
    elif condition == "no_pv":
        runtime.configure(replace(runtime.configuration, sources=(), inverters=()))
    assert forecast_usable(runtime.configuration, runtime.forecast, SITE, now) is usable
    notices = solar_forecast_notifications(runtime.configuration, runtime.forecast, SITE, now)
    assert bool(notices) is not usable
    source, configuration, report = forecast_basis(now, runtime)
    if usable:
        assert source.current(configuration, now, report=report) is not None
    else:
        with pytest.raises(InputsUnavailable, match="^solar_forecast_unavailable$"):
            source.current(configuration, now, report=report)


def test_new_weather_cannot_clear_warning_before_forecast_recovery(solar_runtime):
    """Fresh weather alone retains the warning. A valid forecast clears only solar and preserves another owner."""
    runtime = solar_runtime
    now = START + timedelta(minutes=30)
    location = [SITE]
    center = NotificationCenter()
    center.register(
        "solar",
        lambda: solar_forecast_notifications(
            runtime.configuration, runtime.forecast, location[0], now, "solar_weather_expired"
        ),
        explain=explain_solar_problem,
    )
    center.register(
        "other",
        lambda: (
            Notification("other:one", "input_missing", NoticeText("Other input", "Check it")),
        ),
    )
    first = center.document()["notifications"]
    notice = next(item for item in first if item["owner"] == "solar")
    assert notice["title"] == "Solar forecast is unavailable"
    assert "Weather data for the solar forecast is too old" in notice["message"]
    assert "automatically retries" in notice["message"]
    assert "clears when a valid, up-to-date solar forecast is available" in notice["message"]
    assert "prices" not in notice["message"]
    runtime.set_weather(SITE, weather(now))
    assert any(item["owner"] == "solar" for item in center.document()["notifications"])
    location[0] = None
    unknown = center.document()
    assert "solar" in unknown["unavailable_sources"]
    assert next(item for item in unknown["notifications"] if item["owner"] == "solar")["stale"]
    location[0] = SITE
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert [item["owner"] for item in center.document()["notifications"]] == ["other"]


def test_solar_warning_deduplicates_and_publication_recovery_remains_honest(solar_runtime):
    """One solar cause renders once. Fresh solar permits calculation but does not claim successful publication."""
    runtime = solar_runtime
    now = START + timedelta(minutes=30)
    state = [PlanningStatus("not_ready", "solar_forecast_unavailable", START)]
    center = NotificationCenter()
    center.register(
        "planning",
        lambda: planning_notifications(state[0], center.explain),
        explain=explain_planning_problem,
        fallback=True,
    )
    center.register(
        "solar",
        lambda: solar_forecast_notifications(runtime.configuration, runtime.forecast, SITE, now),
        explain=explain_solar_problem,
    )
    notices = center.document()["notifications"]
    assert len(notices) == 1 and notices[0]["owner"] == "solar"
    assert notices[0]["key"] == "planning:solar_forecast_unavailable"
    runtime.set_weather(SITE, weather(now))
    assert runtime.install(runtime.build(runtime.job(now)), now)
    state[0] = PlanningStatus("calculating", None, START, "solar_forecast_unavailable")
    recovering = center.document()["notifications"]
    assert len(recovering) == 1 and recovering[0]["state"] == "recovering"
    assert "not yet completed successfully" in recovering[0]["message"]
    state[0] = PlanningStatus("ready", "validated", None)
    assert center.document()["notifications"] == []
    assert solar_forecast_notifications(None, None, None, now) == ()
    assert explain_solar_problem("market_prices_unavailable") is None


def test_real_solar_notification_source_clears_after_automatic_forecast_refresh(
    tmp_path, monkeypatch
):
    """The real HTTP source reports failed weather and clears after a background forecast succeeds, without user writes."""
    available = [False]
    monkeypatch.setattr("open_energy_planner.solar.api.SOLAR_WORKER_POLL_SECONDS", 0.01)
    monkeypatch.setattr("open_energy_planner.solar.api.WEATHER_RETRY_MINUTES", 0)

    class WeatherSource:
        """Supply a controlled outage followed by complete current weather without external HTTP."""

        async def read(self, _location, now):
            """Fail before recovery, then return complete physical forecasting evidence."""
            if not available[0]:
                raise OSError("Test weather outage")
            return weather(now)

    @asynccontextmanager
    async def weather_factory():
        """Borrow one isolated provider with no credentials or persistent configuration."""
        yield WeatherSource()

    async def scenario():
        """Exercise actual source registration and recovery through the authenticated endpoint."""
        async with running_forecasts(tmp_path, weather_factory=weather_factory) as (session, url):
            await configure_forecasts(session, url, solar_definition=asdict(plant()))
            async with session.get(url + "/v1/notifications") as response:
                document = await response.json()
            notice = next(item for item in document["notifications"] if item["owner"] == "solar")
            assert notice["title"] == "Solar forecast is unavailable"
            assert notice["blocks"] == ["planning"]
            available[0] = True
            async with asyncio.timeout(15):
                while True:
                    async with session.get(url + "/v1/notifications") as response:
                        recovered = await response.json()
                    if not any(item["owner"] == "solar" for item in recovered["notifications"]):
                        break
                    await asyncio.sleep(0.05)
            async with session.get(url + "/v1/solar") as response:
                solar = await response.json()
            assert solar["forecast"] is not None
            assert solar["issue"] is None
            assert datetime.fromisoformat(solar["forecast"]["issued_at"]) <= datetime.now(UTC)

    asyncio.run(scenario())
