"""Independent price normalization, precedence, scheduling and archive scenarios."""

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiohttp import ClientSession, web

from open_energy_planner.app import create_app
from open_energy_planner.prices.adapters.energy_price_forecast import EnergyPriceForecast
from open_energy_planner.prices.adapters.json_client import JsonClient
from open_energy_planner.prices.adapters.nord_pool import NordPoolPrices
from open_energy_planner.prices.model import (
    MAX_PRICE_POINTS,
    MAX_PRICE_SERIES,
    PriceCoverage,
    PriceKind,
    PricePoint,
    PriceSeries,
    PricesUnavailable,
    combine,
)
from open_energy_planner.prices.service import PriceService, PublicationSchedule
from open_energy_planner.prices.storage.adapters.store import PriceStore
from tests.database.support.archive import (
    corrupt_document,
    list_documents,
    make_store,
    read_document,
)

NOW = datetime(2026, 10, 20, 0, tzinfo=UTC)


def local(hour, minute=0):
    """Return an aware Amsterdam test clock converted to UTC."""
    return datetime(2026, 10, 20, hour, minute, tzinfo=ZoneInfo("Europe/Amsterdam")).astimezone(UTC)


def published(day, now, *, value=0.2):
    """Build a full final delivery day with real 23/24/25-hour bounds and quarter-hour prices."""
    start, end = PublicationSchedule().bounds(day)
    points = tuple(
        PricePoint(
            start + timedelta(minutes=15 * index),
            start + timedelta(minutes=15 * (index + 1)),
            value,
            PriceKind.PUBLISHED,
            15,
        )
        for index in range(int((end - start).total_seconds() / 900))
    )
    return PriceSeries("market_test", "NL", "EUR", now, now, None, points)


def forecast(now=NOW):
    """Provide 48 independent hourly price estimates, usable for six hours after issue."""
    return PriceSeries(
        "forecast_test",
        "NL",
        "EUR",
        now,
        now,
        now + timedelta(hours=6),
        tuple(
            PricePoint(
                now + timedelta(hours=index),
                now + timedelta(hours=index + 1),
                0.4,
                PriceKind.FORECAST,
                60,
            )
            for index in range(48)
        ),
    )


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf")])
def test_invalid_prices_are_not_zero(value):
    """Supply absent or non-finite prices. Reject them while legitimate negative prices remain supported."""
    with pytest.raises(ValueError):
        PricePoint(NOW, NOW + timedelta(minutes=15), value, PriceKind.PUBLISHED, 15)


def test_equal_integer_and_float_prices_have_identical_versions(tmp_path):
    """Receive numerically equal integer/float prices. Normalize their representation so fetch formatting cannot trigger a replan."""
    store = make_store(tmp_path, PriceStore)
    point = PricePoint(NOW, NOW + timedelta(hours=1), 1, PriceKind.FORECAST, 60)
    first = replace(forecast(), points=(point,))
    second = replace(first, points=(replace(point, value=1.0),))
    assert first.version == second.version
    store.save((first,))
    service = PriceService("NL", None, None, store)
    version = service.version
    service.series = (second,)
    assert service.version == version


def test_corrupted_price_archive_is_not_silently_loaded(tmp_path):
    """Modify an archived price behind its content-addressed reference. Reject the corrupted evidence instead of accepting a different price."""
    import json

    store = make_store(tmp_path, PriceStore)
    value = forecast()
    store.save((value,))
    path = f"{value.version}.json"
    data = json.loads(read_document(tmp_path, "prices", path))
    data["points"][0]["value"] = 123
    corrupt_document(tmp_path, "prices", path, json.dumps(data))
    with pytest.raises(ValueError, match="hash mismatch"):
        store.load()


def test_equal_price_forecast_recovery_reports_availability_change(tmp_path):
    """Recover an expired forecast with equal prices. Signal renewed usable coverage even though semantic price values are unchanged."""

    async def scenario():
        """Recover an expired forecast with equal prices. Signal renewed usable coverage even though semantic price values are unchanged."""
        store = make_store(tmp_path, PriceStore)
        expired = replace(
            forecast(), issued_at=NOW - timedelta(hours=7), expires_at=NOW - timedelta(hours=1)
        )
        store.save((expired,))

        class Source:
            """Supply fresh validity metadata for unchanged forecast values."""

            async def fetch(self, now):
                """Supply fresh issue/validity metadata over the unchanged future prices."""
                return forecast(now)

        service = PriceService("NL", None, Source(), store)
        version = service.version
        assert service.timeline(NOW).status == "unavailable"
        assert await service.refresh(NOW)
        assert service.version == version
        assert service.timeline(NOW).status == "ready"

    asyncio.run(scenario())


def test_publication_overlays_hourly_forecast_with_provenance():
    """Replace one forecast quarter by -0.1 published. Preserve hourly origin elsewhere and all 48 hours of coverage."""
    official = replace(
        forecast(),
        source_id="market_test",
        expires_at=None,
        points=(
            PricePoint(
                NOW + timedelta(minutes=15),
                NOW + timedelta(minutes=30),
                -0.1,
                PriceKind.PUBLISHED,
                15,
            ),
        ),
    )
    result = combine((forecast(), official), NOW, market_area="NL")
    assert result.status == "ready"
    assert result.covered_until == NOW + timedelta(hours=48)
    assert [price_entry.value for price_entry in result.entries[:3]] == [0.4, -0.1, 0.4]
    assert [price_entry.native_resolution_minutes for price_entry in result.entries[:3]] == [
        60,
        15,
        60,
    ]
    assert result.entries[1].source_version == official.version
    assert result.entries[0].kind == PriceKind.FORECAST


def test_expiry_is_source_age_not_forecast_horizon_and_gaps_stop_coverage():
    """Use a fresh six-hour-valid forecast over 48 hours. After expiry mark it unavailable, and never bridge a gap."""
    assert combine((forecast(),), NOW, market_area="NL").status == "ready"
    assert (
        combine((forecast(),), NOW + timedelta(hours=6), market_area="NL").status == "unavailable"
    )
    incomplete = replace(forecast(), points=(forecast().points[0], forecast().points[2]))
    result = combine((incomplete,), NOW, market_area="NL")
    assert result.status == "shortened_horizon"
    assert result.covered_until == NOW + timedelta(hours=1)


@pytest.mark.parametrize("change", [{"market_area": "BE"}, {"currency": "USD"}])
def test_cross_market_or_currency_series_are_rejected(change):
    """Mix an incompatible area or currency. Fail composition rather than silently using another market's price."""
    with pytest.raises(ValueError, match="mismatch"):
        combine((replace(forecast(), **change),), NOW, market_area="NL")


@pytest.mark.parametrize(
    "day, quarters", [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100), (date(2026, 10, 20), 96)]
)
def test_complete_day_uses_dst_bounds_instead_of_96_slots(tmp_path, day, quarters):
    """Use a spring, autumn or ordinary delivery day. Accept exactly its 92/100/96 contiguous quarters."""
    store = make_store(tmp_path, PriceStore)
    series = published(day, NOW)
    store.save((series,))
    service = PriceService("NL", None, None, store)
    assert len(series.points) == quarters
    assert service.complete(day)


def test_daily_fetch_retries_only_half_hourly_and_checks_corrections_once(tmp_path):
    """Expect tomorrow after 13:05. Retry at 13:35/14:05, stop on completion and check once at 16:05."""

    async def scenario():
        """Expect tomorrow after 13:05. Retry at 13:35/14:05, stop on completion and check once at 16:05."""
        store = make_store(tmp_path, PriceStore)
        store.save((published(date(2026, 10, 20), local(12)),))

        class Market:
            """Record publication attempts and simulate delayed final prices."""

            def __init__(self):
                """Record exact attempts to distinguish fetching from frequent planner/poll calls."""
                self.calls = []

            async def fetch_day(self, day, now):
                """Simulate delayed tomorrow publication until the 14:05 request."""
                self.calls.append((day, now))
                if now < local(14, 5):
                    raise PricesUnavailable("not_published")
                return published(day, now)

        source = Market()
        service = PriceService("NL", source, None, store)
        assert not await service.refresh(local(13, 4))
        assert not source.calls
        for hour, minute in (
            (13, 5),
            (13, 10),
            (13, 34),
            (13, 35),
            (14, 4),
            (14, 5),
            (14, 35),
            (16, 4),
            (16, 5),
            (16, 6),
        ):
            await service.refresh(local(hour, minute))
        assert [now for _, now in source.calls] == [
            local(13, 5),
            local(13, 35),
            local(14, 5),
            local(16, 5),
        ]
        version = service.version
        assert not await service.refresh(local(17))
        assert service.version == version
        assert not service.issues
        restored = PriceService("NL", source, None, make_store(tmp_path, PriceStore))
        assert restored.complete(date(2026, 10, 21))
        assert not await restored.refresh(local(15))
        assert len(source.calls) == 4

    asyncio.run(scenario())


def test_incomplete_correction_and_save_failure_preserve_accepted_prices(tmp_path, monkeypatch):
    """Reject a partial correction or failed pointer save. Retain current values and exact immutable archive bytes."""

    async def scenario():
        """Reject a partial correction or failed pointer save. Retain current values and exact immutable archive bytes."""
        store = make_store(tmp_path, PriceStore)
        original = published(date(2026, 10, 20), local(12))
        store.save((original,))
        path = f"{original.version}.json"
        archived = read_document(tmp_path, "prices", path)
        service = PriceService("NL", None, None, store)
        partial = replace(original, points=original.points[:-1])
        with pytest.raises(PricesUnavailable, match="incomplete"):
            await service._accept(partial, date(2026, 10, 20))
        corrected = published(date(2026, 10, 20), local(13), value=-0.1)

        real_replace = store.documents.write

        def fail_pointer(source, target):
            """Fail only the cache pointer replacement after the new immutable version was safely written."""
            if source == "current.json":
                raise OSError("Injected price-pointer failure")
            real_replace(source, target)

        with monkeypatch.context() as fault:
            fault.setattr(store.documents, "write", fail_pointer)
            with pytest.raises(OSError):
                await service._accept(corrected, date(2026, 10, 20))
        assert service.series == (original,)
        assert make_store(tmp_path, PriceStore).load() == (original,)
        assert read_document(tmp_path, "prices", path) == archived
        await service._accept(corrected, date(2026, 10, 20))
        assert service.series == (corrected,)
        assert read_document(tmp_path, "prices", path) == archived

    asyncio.run(scenario())


def test_forecast_fetch_is_hourly_while_plans_can_read_cached_prices_each_quarter(tmp_path):
    """Read prices every quarter. Request forecasts only initially and hourly, with no price-version churn for identical values."""

    async def scenario():
        """Read prices every quarter. Request forecasts only initially and hourly, with no price-version churn for identical values."""

        class Forecast:
            """Count forecast refreshes while preserving numerical prices."""

            calls = 0

            async def fetch(self, now):
                """Return unchanged prices with a newly reported model issue time."""
                self.calls += 1
                return replace(
                    forecast(), received_at=now, issued_at=now, expires_at=now + timedelta(hours=6)
                )

        class Market:
            """Supply final day coverage independently of forecast timing."""

            async def fetch_day(self, day, now):
                """Supply complete published coverage so retries do not obscure forecast timing."""
                return published(day, now)

        source = Forecast()
        service = PriceService("NL", Market(), source, make_store(tmp_path, PriceStore))
        assert await service.refresh(NOW)
        version = service.version
        for minute in (15, 30, 45, 60):
            changed = await service.refresh(NOW + timedelta(minutes=minute))
            # The original 48-hour forecast becomes a shortened rolling horizon
            # at the first quarter. Unchanged issue metadata adds no other change.
            assert changed is (minute == 15)
            assert service.timeline(NOW + timedelta(minutes=minute)).entries
        assert source.calls == 2
        assert service.version == version

    asyncio.run(scenario())


class ResponseFixture:
    """Supply a hand-authored source response and capture outbound request parameters."""

    def __init__(self, payload):
        """Bind isolated JSON without contacting an external price service."""
        self.payload, self.calls = payload, []

    async def get(self, url, params):
        """Record the adapter call and return the explicit fixture object."""
        self.calls.append((url, params))
        return self.payload


@pytest.mark.parametrize(
    "area, provider_code",
    [
        ("NL", "NL"),
        ("DK1", "DK1"),
        ("DK2", "DK2"),
        ("SE1", "SE1"),
        ("SE4", "SE4"),
        ("NO1", "NO1"),
        ("NO5", "NO5"),
        ("DE-LU", "GER"),
    ],
)
def test_published_adapter_preserves_bidding_zone_and_translates_only_provider_alias(
    area, provider_code
):
    """Request an explicit region. Preserve its internal identity while translating the German source alias inside the adapter only."""
    client = ResponseFixture(
        {
            "currency": "EUR",
            "market": "DayAhead",
            "deliveryDateCET": "2026-10-20",
            "updatedAt": NOW.isoformat(),
            "areaStates": [{"state": "Final", "areas": [provider_code]}],
            "multiAreaEntries": [
                {
                    "deliveryStart": NOW.isoformat(),
                    "deliveryEnd": (NOW + timedelta(minutes=15)).isoformat(),
                    "entryPerArea": {provider_code: 100},
                }
            ],
        }
    )
    result = asyncio.run(NordPoolPrices(client, area).fetch_day(date(2026, 10, 20), NOW))
    assert client.calls[0][1]["deliveryArea"] == provider_code
    assert result.market_area == area
    assert result.points[0].value == 0.1


@pytest.mark.parametrize(
    "area, provider_code",
    [
        ("NL", "nl"),
        ("DK1", "dk1"),
        ("DK2", "dk2"),
        ("SE1", "se1"),
        ("SE4", "se4"),
        ("NO1", "no1"),
        ("NO5", "no5"),
        ("DE-LU", "de"),
    ],
)
def test_forecast_adapter_preserves_exact_bidding_zone(area, provider_code):
    """Fetch one zone's forecast. Keep DK1/DK2 and other regional identities separate rather than falling back to a country average."""
    client = ResponseFixture(
        {
            "country": provider_code.upper(),
            "currency": "EUR",
            "unit": "EUR/kWh",
            "source": {"firestore": {"updated_at": NOW.isoformat()}},
            "entries": [
                {
                    "start": NOW.isoformat(),
                    "end": (NOW + timedelta(hours=1)).isoformat(),
                    "value": 0.1,
                    "unit": "EUR/kWh",
                    "source": "forecast",
                    "native_resolution_minutes": 60,
                }
            ],
        }
    )
    result = asyncio.run(EnergyPriceForecast(client, area).fetch(NOW))
    assert client.calls[0][1]["country"] == provider_code
    assert client.calls[0][1]["currency"] == "EUR"
    assert result.market_area == area
    client.payload["country"] = "DK2" if area == "DK1" else "wrong_zone"
    with pytest.raises(PricesUnavailable):
        asyncio.run(EnergyPriceForecast(client, area).fetch(NOW))


@pytest.mark.parametrize("status", [200, 302, 429])
@pytest.mark.parametrize("method", ["get", "post"])
def test_json_transport_bounds_requests_and_refuses_redirects(status, method):
    """Serve fragmented JSON or a redirect/rate limit. Accept complete JSON and reject non-200 without following redirects."""

    async def scenario():
        """Serve fragmented JSON or a redirect/rate limit. Accept complete JSON and reject non-200 without following redirects."""

        async def handler(request):
            """Return a deliberately fragmented valid body or the explicit HTTP failure."""
            assert "Authorization" not in request.headers
            if status != 200:
                return web.Response(status=status, headers={"Location": "http://127.0.0.1:1/never"})
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b'{"entries":')
            await asyncio.sleep(0.01)
            await response.write(b"[]}")
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_route(method.upper(), "/prices", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            async with ClientSession() as session:
                client = JsonClient(session)
                url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/prices"
                if status == 200:
                    assert await getattr(client, method)(url, {}) == {"entries": []}
                else:
                    with pytest.raises(PricesUnavailable, match=f"price_http_{status}"):
                        await getattr(client, method)(url, {})
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_equal_price_publication_changes_kind_without_mutating_old_timeline(tmp_path):
    """Publish identical numeric prices. Change provenance/version and retain the earlier forecast-only timeline unchanged."""

    async def scenario():
        """Publish identical numeric prices. Change provenance/version and retain the earlier forecast-only timeline unchanged."""
        store = make_store(tmp_path, PriceStore)
        store.save((forecast(),))
        service = PriceService("NL", None, None, store)
        old = service.timeline(NOW)
        version = service.version
        official = published(date(2026, 10, 20), NOW, value=0.4)
        await service._accept(official, date(2026, 10, 20))
        new = service.timeline(NOW)
        assert new.entries[0].value == old.entries[0].value == 0.4
        assert old.entries[0].kind == PriceKind.FORECAST
        assert new.entries[0].kind == PriceKind.PUBLISHED
        assert service.version != version
        assert old.entries[0].source_version == forecast().version

    asyncio.run(scenario())


def test_forecast_failure_retains_cache_without_extending_expiry(tmp_path):
    """Fail forecast refresh after source expiry. Retain archive bytes but expose only published coverage, never renewed forecast validity."""

    async def scenario():
        """Fail forecast refresh after source expiry. Retain archive bytes but expose only published coverage, never renewed forecast validity."""
        store = make_store(tmp_path, PriceStore)
        original = forecast()
        store.save((original, published(date(2026, 10, 20), NOW)))

        class Forecast:
            """Simulate a forecast outage after cached model expiry."""

            async def fetch(self, now):
                """Model an external outage without changing the previous model issue time."""
                raise PricesUnavailable("fixture_outage")

        service = PriceService("NL", None, Forecast(), store)
        await service.refresh(NOW + timedelta(hours=6))
        result = service.timeline(NOW + timedelta(hours=6))
        assert result.status == "shortened_horizon"
        assert all(price_entry.kind == PriceKind.PUBLISHED for price_entry in result.entries)
        assert service.issues["forecast"] == "fixture_outage"
        assert store.load()[0] == original

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["Final", "Preliminary"])
def test_nord_pool_converts_mwh_once_and_rejects_preliminary(state):
    """Normalize -100 EUR/MWh to -0.1 EUR/kWh only for final NL prices. Reject preliminary results."""
    payload = {
        "currency": "EUR",
        "market": "DayAhead",
        "deliveryDateCET": "2026-10-20",
        "updatedAt": NOW.isoformat(),
        "areaStates": [{"state": state, "areas": ["NL"]}],
        "multiAreaEntries": [
            {
                "deliveryStart": NOW.isoformat(),
                "deliveryEnd": (NOW + timedelta(minutes=15)).isoformat(),
                "entryPerArea": {"NL": -100},
            }
        ],
    }
    adapter = NordPoolPrices(ResponseFixture(payload), "NL")
    if state == "Final":
        result = asyncio.run(adapter.fetch_day(date(2026, 10, 20), NOW))
        assert result.points[0].value == -0.1
        assert result.points[0].native_resolution_minutes == 15
    else:
        with pytest.raises(PricesUnavailable, match="not_final"):
            asyncio.run(adapter.fetch_day(date(2026, 10, 20), NOW))


@pytest.mark.parametrize("offset", [-5, 2])
def test_equal_timezone_instants_roundtrip_with_identical_archive_version(tmp_path, offset):
    """Save equivalent instants with different offsets. Normalize to UTC and reload the same immutable version."""
    original = forecast()
    zone = timezone(timedelta(hours=offset))
    shifted = replace(
        original,
        received_at=original.received_at.astimezone(zone),
        issued_at=original.issued_at.astimezone(zone),
        expires_at=original.expires_at.astimezone(zone),
        points=tuple(
            replace(
                point_item,
                start=point_item.start.astimezone(zone),
                end=point_item.end.astimezone(zone),
            )
            for point_item in original.points
        ),
    )
    assert shifted.version == original.version
    store = make_store(tmp_path, PriceStore)
    store.save((shifted,))
    assert store.load() == (original,)


def test_expiry_between_refresh_calls_signals_once_without_fetch(tmp_path):
    """Let cached forecasts expire between polls. Signal lost coverage once, without a failed fetch or repeated empty-horizon churn."""

    async def scenario():
        """Let cached forecasts expire between polls. Signal lost coverage once, without a failed fetch or repeated empty-horizon churn."""
        store = make_store(tmp_path, PriceStore)
        store.save((forecast(),))
        service = PriceService("NL", None, None, store)
        assert not await service.refresh(NOW)
        assert await service.refresh(NOW + timedelta(hours=6))
        assert not await service.refresh(NOW + timedelta(hours=6, seconds=30))

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["area", "corruption", "null", "array", "scalar"])
def test_cache_problem_does_not_abort_app_startup(tmp_path, fault):
    """Change area or corrupt a cache. Keep authenticated app endpoints available with explicit unavailable-price diagnostics."""

    async def scenario():
        """Change area or corrupt a cache. Keep authenticated app endpoints available with explicit unavailable-price diagnostics."""
        store = make_store(tmp_path, PriceStore)
        store.save((forecast(),))
        if fault == "corruption":
            corrupt_document(tmp_path, "prices", store.current_name, "broken cache")
        elif fault in ("null", "array", "scalar"):
            corrupt_document(
                tmp_path,
                "prices",
                f"{forecast().version}.json",
                {"null": "null", "array": "[]", "scalar": "42"}[fault],
            )
        area = "BE" if fault == "area" else "NL"
        before = {
            name: read_document(tmp_path, "prices", name)
            for name in list_documents(tmp_path, "prices")
        }

        @asynccontextmanager
        async def factory(_database_engine):
            """Load the changed installation without fabricating usable prices."""
            yield PriceService(area, None, None, store)

        runner = web.AppRunner(
            create_app(
                tmp_path,
                "local-test-only-0123456789abcdef",
                price_factory=factory,
                source="home_assistant",
            )
        )
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession(
                headers={"Authorization": "Bearer local-test-only-0123456789abcdef"}
            ) as session:
                async with session.get(url + "/v1/prices") as response:
                    data = await response.json()
                    assert data["status"] == "unavailable"
                    assert data["issues"]["cache"] == (
                        "cache_identity_mismatch" if fault == "area" else "cache_unreadable"
                    )
                async with session.get(url + "/v1/snapshot") as response:
                    assert response.status == 200
        finally:
            await runner.cleanup()
        assert before == {
            name: read_document(tmp_path, "prices", name)
            for name in list_documents(tmp_path, "prices")
        }

    asyncio.run(scenario())


def test_obsolete_correction_issue_clears_after_day_rollover(tmp_path):
    """Fail a correction for an already complete tomorrow. Clear that obsolete issue when tomorrow becomes today."""

    async def scenario():
        """Fail a correction for an already complete tomorrow. Clear that obsolete issue when tomorrow becomes today."""
        store = make_store(tmp_path, PriceStore)
        store.save(
            (published(date(2026, 10, 20), local(16)), published(date(2026, 10, 21), local(16)))
        )

        class Market:
            """Fail an optional correction while complete final prices remain cached."""

            async def fetch_day(self, day, now):
                """Fail the optional correction without invalidating complete published prices."""
                raise PricesUnavailable("fixture_correction_failure")

        service = PriceService("NL", Market(), None, store)
        await service.refresh(local(16, 5))
        assert service.issues["published:2026-10-21"]
        await service.refresh(local(0, 5) + timedelta(days=1))
        assert not service.issues

    asyncio.run(scenario())


def test_optional_model_issue_metadata_stays_unknown_and_response_age_is_bounded():
    """Omit undocumented model metadata. Accept a schema-conforming response with unknown issue time and a one-hour response-age bound."""
    client = ResponseFixture(
        {
            "country": "NL",
            "unit": "EUR/kWh",
            "currency": "EUR",
            "generated_at": NOW.isoformat(),
            "entries": [
                {
                    "start": NOW.isoformat(),
                    "end": (NOW + timedelta(hours=1)).isoformat(),
                    "value": 0.1,
                    "unit": "EUR/kWh",
                    "source": "forecast",
                    "native_resolution_minutes": 60,
                }
            ],
        }
    )
    del client.payload["entries"][0]["native_resolution_minutes"]
    adapter = EnergyPriceForecast(client, "NL")
    result = asyncio.run(adapter.fetch(NOW))
    assert result.issued_at is None
    assert result.points[0].native_resolution_minutes == 60
    assert result.expires_at == NOW + timedelta(hours=1)
    with pytest.raises(PricesUnavailable, match="price_forecast_expired"):
        asyncio.run(adapter.fetch(NOW + timedelta(hours=1)))
    client.payload["source"] = {"firestore": {"updated_at": (NOW - timedelta(hours=6)).isoformat()}}
    with pytest.raises(PricesUnavailable, match="price_forecast_expired"):
        asyncio.run(adapter.fetch(NOW))


@pytest.mark.parametrize("unit", ["EUR/kWh", "EUR/MWh"])
def test_forecast_adapter_requests_native_base_and_keeps_hourly_origin(unit):
    """Request forecast-only native base prices. Accept EUR/kWh, preserve 60-minute origin and reject changed units."""
    client = ResponseFixture(
        {
            "country": "NL",
            "currency": "EUR",
            "unit": "EUR/kWh",
            "source": {"firestore": {"updated_at": NOW.isoformat()}},
            "entries": [
                {
                    "start": NOW.isoformat(),
                    "end": (NOW + timedelta(hours=1)).isoformat(),
                    "value": -0.1,
                    "unit": unit,
                    "source": "forecast",
                    "native_resolution_minutes": 60,
                }
            ],
        }
    )
    adapter = EnergyPriceForecast(client, "NL")
    if unit == "EUR/kWh":
        result = asyncio.run(adapter.fetch(NOW))
        assert result.points[0].value == -0.1
        assert result.points[0].native_resolution_minutes == 60
        assert client.calls[0][1]["mode"] == "forecast_only"
        assert client.calls[0][1]["resolution"] == "native"
    else:
        with pytest.raises(PricesUnavailable):
            asyncio.run(adapter.fetch(NOW))


@pytest.mark.parametrize(
    "forecast_hour_count, expected_status",
    [(0, "unavailable"), (2, "shortened_horizon"), (72, "ready")],
)
def test_price_endpoint_is_authenticated_and_serves_cached_normalized_data(
    tmp_path, forecast_hour_count, expected_status
):
    """Serialize all three coverage states through authenticated HTTP with unchanged wire strings."""

    async def scenario():
        """Run a real local app HTTP server. Reject anonymous price access and expose cached provenance without provider JSON."""
        forecast_start = datetime.now(UTC) - timedelta(hours=1)
        store = make_store(tmp_path, PriceStore)
        points = tuple(
            PricePoint(
                forecast_start + timedelta(hours=forecast_hour_index),
                forecast_start + timedelta(hours=forecast_hour_index + 1),
                0.4,
                PriceKind.FORECAST,
                60,
            )
            for forecast_hour_index in range(forecast_hour_count)
        )
        store.save((replace(forecast(forecast_start), points=points),) if points else ())
        service = PriceService("NL", None, None, store)
        assert service.timeline(datetime.now(UTC)).status is PriceCoverage(expected_status)

        @asynccontextmanager
        async def factory(_database_engine):
            """Yield a cached service without enabling external network access in CI."""
            yield service

        runner = web.AppRunner(
            create_app(
                tmp_path,
                "local-test-only-0123456789abcdef",
                price_factory=factory,
                source="home_assistant",
            )
        )
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1/prices"
        try:
            async with ClientSession() as session:
                async with session.get(url) as response:
                    assert response.status == 401
                async with session.get(
                    url, headers={"Authorization": "Bearer local-test-only-0123456789abcdef"}
                ) as response:
                    assert response.status == 200
                    data = await response.json()
                    assert data["status"] == expected_status
                    assert data["selection_policy"]["price_coverage_hours"] == 72
                    if forecast_hour_count:
                        assert data["entries"][0]["kind"] == "forecast"
                        assert data["entries"][0]["native_resolution_minutes"] == 60
                        assert data["entries"][0]["source_version"]
                    else:
                        assert data["entries"] == []
                    assert data["market_area"] == "NL"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["area", "pointer"])
def test_cache_issue_clears_only_after_durable_recovery(tmp_path, fault):
    """Replace a foreign cache or broken pointer with valid prices. Clear the recovered diagnostic."""

    async def scenario():
        """Accept a valid forecast and prove both the cache and diagnostic recover together."""
        store = make_store(tmp_path, PriceStore)
        store.save((replace(forecast(), market_area="BE"),))
        if fault == "pointer":
            corrupt_document(tmp_path, "prices", store.current_name, "broken")
        service = PriceService("NL", None, None, store)
        assert service.issues["cache"]
        await service._accept(forecast())
        assert "cache" not in service.issues
        assert store.load() == (forecast(),)

    asyncio.run(scenario())


def test_corrupt_existing_archive_cannot_masquerade_as_recovery(tmp_path):
    """Preserve a corrupt immutable archive and retain the issue when the same version cannot be saved safely."""

    async def scenario():
        """Reject a false repair rather than advancing references to a still-unreadable archive."""
        store = make_store(tmp_path, PriceStore)
        store.save((forecast(),))
        archive = f"{forecast().version}.json"
        corrupt_document(tmp_path, "prices", archive, "null")
        service = PriceService("NL", None, None, store)
        with pytest.raises(OSError, match="archive is unusable"):
            await service._accept(forecast())
        assert service.issues["cache"] == "cache_unreadable"
        assert not service.series
        assert read_document(tmp_path, "prices", archive) == "null"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("country", None),
        ("country", 42),
        ("country", []),
        ("country", {}),
        ("entries", [None]),
        ("entries", [42]),
        ("entries", [[]]),
        ("entries", {}),
        ("entries", []),
        ("entries", [{}] * (MAX_PRICE_POINTS + 1)),
    ],
)
def test_malformed_forecast_reports_readiness_and_retains_accepted_prices(tmp_path, field, value):
    """Reject malformed provider shapes through a sanitized source issue without replacing accepted prices."""

    async def scenario():
        """Refresh a malformed response while preserving the previous valid cached source."""
        payload = {
            "country": "NL",
            "currency": "EUR",
            "unit": "EUR/kWh",
            "generated_at": NOW.isoformat(),
            "entries": [],
        }
        payload[field] = value
        store = make_store(tmp_path, PriceStore)
        store.save((forecast(),))
        service = PriceService(
            "NL", None, EnergyPriceForecast(ResponseFixture(payload), "NL"), store
        )
        await service.refresh(NOW)
        assert service.issues["forecast"] == "invalid_price_forecast"
        assert service.series == (forecast(),)
        assert store.load() == (forecast(),)

    asyncio.run(scenario())


def test_dense_or_oversized_price_sources_are_rejected():
    """Reject sub-quarter density and over-limit point counts before composition can consume excessive work."""
    point = forecast().points[0]
    with pytest.raises(ValueError, match="Invalid price interval"):
        replace(point, end=point.start + timedelta(seconds=1))
    points = tuple(
        replace(point, start=NOW + timedelta(hours=index), end=NOW + timedelta(hours=index + 1))
        for index in range(MAX_PRICE_POINTS + 1)
    )
    with pytest.raises(ValueError, match="512"):
        replace(forecast(), points=points)
    with pytest.raises(ValueError, match="Too many price sources"):
        combine((forecast() for _ in range(MAX_PRICE_SERIES + 1)), NOW, market_area="NL")


def test_maximum_admitted_sources_compose_with_deterministic_priority():
    """Compose eight 512-point sources. The latest issue wins every quarter with no lost coverage."""
    points = tuple(
        PricePoint(
            NOW + timedelta(minutes=15 * index),
            NOW + timedelta(minutes=15 * (index + 1)),
            0.4,
            PriceKind.FORECAST,
            15,
        )
        for index in range(MAX_PRICE_POINTS)
    )
    sources = tuple(
        replace(
            forecast(),
            source_id=f"source_{index}",
            points=points,
            issued_at=NOW - timedelta(minutes=MAX_PRICE_SERIES - index),
        )
        for index in range(MAX_PRICE_SERIES)
    )
    result = combine(sources, NOW, market_area="NL")
    assert result.status == "ready"
    assert len(result.entries) == 192
    assert all(entry.source_id == "source_7" and entry.value == 0.4 for entry in result.entries)


def test_cancelled_accept_waits_for_owned_storage_writer(tmp_path):
    """Cancel acceptance during a disk write. Drain the writer before releasing service ownership."""

    async def scenario():
        """Block the actual storage thread and prove cancellation cannot leave it running after exit."""
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        class BlockingStore(PriceStore):
            """Control disk completion explicitly without contacting external services."""

            def save(self, series):
                """Write only after release and always report thread completion."""
                started.set()
                try:
                    if not release.wait(5):
                        raise TimeoutError("Test writer was not released")
                    super().save(series)
                finally:
                    finished.set()

        base_store = make_store(tmp_path, PriceStore)
        store = BlockingStore(base_store.documents)
        service = PriceService("NL", None, None, store)
        task = asyncio.create_task(service._accept(forecast()))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert not finished.is_set()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert finished.is_set()
        assert store.load() == (forecast(),)
        assert not service.series

    asyncio.run(scenario())


def test_combined_hourly_forecasts_repeat_on_quarters_without_changing_origin():
    """Repeat a negative hourly price four times with exact source versions and 60-minute native resolution.

    Normalization creates no prediction detail and does not mutate raw series.
    Rolling reads clip only their requested first and last quarter.
    """
    original = forecast()
    points = (replace(original.points[0], value=-0.12),)
    original = replace(original, points=points)
    timeline = combine((original,), NOW, market_area="NL", hours=1)
    assert [(entry.start, entry.end) for entry in timeline.entries] == [
        (NOW + timedelta(minutes=minute), NOW + timedelta(minutes=minute + 15))
        for minute in (0, 15, 30, 45)
    ]
    assert all(
        entry.value == -0.12 and entry.native_resolution_minutes == 60 for entry in timeline.entries
    )
    assert all(
        entry.source_version == original.version and entry.source_id == original.source_id
        for entry in timeline.entries
    )
    assert original.points == points and len(original.points) == 1
    clipped = combine((original,), NOW + timedelta(minutes=7), market_area="NL", hours=0.75)
    assert clipped.entries[0].start == NOW + timedelta(minutes=7)
    assert clipped.entries[0].end == NOW + timedelta(minutes=15)
    assert clipped.covered_until == NOW + timedelta(minutes=52)
    assert clipped.status == "ready"
