"""Protect live laboratory adapter composition and isolated deterministic unit-test sources."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.prices.model import PriceKind, combine
from script.provisioning import sample_settings as provision_sample_settings
from tests.test_environment.support.sources import TestPrices as LabPrices
from tests.test_environment.support.sources import TestTemperatures as LabTemperatures
from tests.test_environment.support.sources import TestWeather as LabWeather
from tests.test_environment.support.sources import price_rows


def test_lab_market_fixture_covers_48_hours_and_includes_negative_and_peak_prices():
    """The lab labels published quarters and overlapping hourly forecasts, retaining a 72h tail without extending commands."""
    now = datetime(2026, 10, 21, 0, tzinfo=UTC)
    sources = price_rows(now)
    assert all(source.source_id.startswith("interactive-test-") for source in sources)
    assert len(sources[0].points) == 96 and len(sources[1].points) == 24
    assert all(
        point.kind == PriceKind.PUBLISHED and point.native_resolution_minutes == 15
        for point in sources[0].points
    )
    assert all(
        point.kind == PriceKind.FORECAST and point.native_resolution_minutes == 60
        for point in sources[1].points
    )
    assert min(point.value for source in sources for point in source.points) == -0.2
    assert max(point.value for source in sources for point in source.points) == 0.36
    assert combine(sources, now, market_area="NL").status == "ready"
    assert sources[-1].points[-1].end == now + timedelta(hours=72)


def test_lab_refresh_upgrades_existing_same_quarter_cache_with_extended_source():
    """An existing two-source lab gains the new 72h tail immediately, preserving its old immutable cohort."""

    async def scenario():
        """Restore the previous fixture shape and require one upgrade write without quarter-boundary delay."""
        now = datetime(2026, 10, 21, 0, tzinfo=UTC)
        previous = price_rows(now)[:2]
        writes = []
        store = SimpleNamespace(load=lambda: previous, save=writes.append)
        service = LabPrices("NL", None, None, store)
        assert await service.refresh(now)
        assert len(service.series) == 3
        assert service.series[-1].points[-1].end == now + timedelta(hours=72)
        assert len(previous) == 2 and len(writes) == 1
        assert not await service.refresh(now + timedelta(minutes=1))
        assert len(writes) == 1

    asyncio.run(scenario())


def test_lab_weather_ports_are_offline_and_cover_history_and_future():
    """Both weather ports cover 32 historic and three future days at one declared site. Night irradiance is zero and temperatures agree."""

    async def scenario():
        """Call plain injected sources without any network client or provider credential."""
        now = datetime(2026, 10, 21, 0, tzinfo=UTC)
        site = SiteLocation(52, 6, "Europe/Amsterdam")
        solar = await LabWeather().read(site, now)
        demand = await LabTemperatures().read(site, now)
        assert len(solar) == len(demand) == 35 * 24
        assert solar[0].start == now - timedelta(days=32)
        assert solar[-1].start == now + timedelta(days=3, hours=-1)
        assert all(row.celsius == 15 for row in demand)
        assert solar[0].ghi == 0
        assert max(row.ghi for row in solar) > 600

    asyncio.run(scenario())


def test_lab_price_write_yields_event_loop_and_drains_on_cancellation():
    """A blocked disk writer runs off-loop. Cancellation waits for it and never publishes uncommitted prices."""

    async def scenario():
        """Use thread barriers so cancellation and event-loop responsiveness are checked without timing guesses."""
        started, release, finished = Event(), Event(), Event()
        loop_thread = get_ident()

        def save(_rows):
            """Block only the worker until the test permits the durable write to finish."""
            assert get_ident() != loop_thread
            started.set()
            assert release.wait(5)
            finished.set()

        service = LabPrices("NL", None, None, SimpleNamespace(load=lambda: (), save=save))
        task = asyncio.create_task(service.refresh(datetime(2026, 10, 21, tzinfo=UTC)))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert service.series == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("partial", [True, False])
def test_lab_restart_preserves_custom_settings_and_reports_partial_setup(
    monkeypatch, capsys, partial
):
    """A restart never combines sample devices with existing user settings. Incomplete setups explicitly require the wizard."""
    calls = []
    setup = {
        "catalogue": {
            "data": {
                "suggestions": {
                    "energy_status": "ready",
                    "terms": [{"entity_id": "sensor.oep_test_grid_import"}],
                }
            }
        },
        "annual_estimate": {"days": 28},
        "demand": {"user_value": "preserve"},
        "solar": None if partial else {"sources": []},
        "installation": None if partial else {"batteries": []},
    }

    @asynccontextmanager
    async def request(url, **kwargs):
        """Expose saved settings. Reject unexpected provisioning writes by recording every call."""
        calls.append(url)

        async def json_body():
            """Return only the catalogue or the already-configured contract."""
            return {"status": "configured"} if url.endswith("contracts") else setup

        yield SimpleNamespace(raise_for_status=lambda: None, json=json_body)

    @asynccontextmanager
    async def session(**kwargs):
        """Substitute the provisioner's HTTP boundary without touching any real installation."""
        yield SimpleNamespace(get=request)

    monkeypatch.setenv("OEP_API_TOKEN", "test-only")
    monkeypatch.setattr(provision_sample_settings, "ClientSession", session)
    asyncio.run(provision_sample_settings.provision(app_url="http://app:8099", token="test-only"))
    assert len(calls) == 3
    output = capsys.readouterr().out
    assert ("Partial setup preserved" in output) == partial
    assert setup["demand"] == {"user_value": "preserve"}


@pytest.mark.parametrize("legacy", [True, False])
def test_automatic_provisioning_waits_for_new_or_migrated_supervisor_projection(legacy):
    """An active legacy status is insufficient. A fresh or retained entry must publish successful Supervisor ownership."""
    from script.home_assistant.bootstrap.provision_ha import wait_for_automatic

    async def scenario():
        """Advance API evidence through absent/legacy, reconnecting and confirmed ownership. Never use HA database files."""
        round_number = 0

        async def request(method, endpoint):
            """Return independent status fixtures. Only the third response proves automatic ownership."""
            nonlocal round_number
            assert method == "GET"
            if endpoint.endswith("/entry"):
                return [
                    {"domain": "hassio", "state": "loaded"},
                    *(
                        []
                        if not legacy and round_number == 0
                        else [{"domain": "open_energy_planner"}]
                    ),
                ]
            round_number += 1
            return [
                {
                    "entity_id": "sensor.open_energy_planner_status",
                    "state": "active",
                    "attributes": {
                        "connection_type": "standalone" if round_number == 1 else "supervisor",
                        "connection_issue": "supervisor_reconnecting"
                        if round_number == 2
                        else None,
                    },
                }
            ]

        await wait_for_automatic(request, attempts=3, delay=0)
        assert round_number == 3

    asyncio.run(scenario())


def test_automatic_provisioning_rejects_active_legacy_and_duplicate_entries():
    """A working manual connection cannot falsely pass HA OS acceptance, and duplicate entries fail explicitly."""
    from script.home_assistant.bootstrap.provision_ha import wait_for_automatic

    async def scenario():
        """Keep legacy status active across every retry, then exercise the independent duplicate boundary."""

        async def request(method, endpoint):
            """Expose an existing active manual integration. No response confirms automatic ownership."""
            return (
                [{"domain": "open_energy_planner"}]
                if endpoint.endswith("/entry")
                else [
                    {
                        "entity_id": "sensor.open_energy_planner_status",
                        "state": "active",
                        "attributes": {"connection_type": "standalone"},
                    }
                ]
            )

        with pytest.raises(RuntimeError, match="did not become ready"):
            await wait_for_automatic(request, attempts=2, delay=0)

        async def duplicates(method, endpoint):
            """Return two OEP entries so automatic provisioning cannot silently choose one."""
            return [{"domain": "open_energy_planner"}] * 2

        with pytest.raises(RuntimeError, match="Duplicate"):
            await wait_for_automatic(duplicates, attempts=1, delay=0)

    asyncio.run(scenario())


@pytest.mark.parametrize("initial_state", [None, "not_loaded", "setup_in_progress", "setup_retry"])
@pytest.mark.parametrize("completes", [False, True])
def test_automatic_readiness_awaits_hassio_setup_even_with_active_retained_app(
    initial_state, completes
):
    """An already-connected OEP app cannot hide pending Hassio setup or its scheduled platform update."""
    from script.home_assistant.bootstrap.provision_ha import wait_for_automatic

    async def scenario():
        """Keep OEP healthy throughout and admit readiness only after the independent platform entry loads."""
        attempts = 0

        async def request(method, endpoint):
            """Expose platform setup independently of the retained app's healthy status."""
            nonlocal attempts
            if endpoint.endswith("/entry"):
                attempts += 1
                state = "loaded" if completes and attempts == 2 else initial_state
                return [
                    {"domain": "open_energy_planner"},
                    *([{"domain": "hassio", "state": state}] if state is not None else []),
                ]
            return [
                {
                    "entity_id": "sensor.open_energy_planner_status",
                    "attributes": {"connection_type": "supervisor"},
                }
            ]

        if completes:
            await wait_for_automatic(request, attempts=2, delay=0)
        else:
            with pytest.raises(RuntimeError, match="did not become ready"):
                await wait_for_automatic(request, attempts=2, delay=0)
        assert attempts == 2

    asyncio.run(scenario())
