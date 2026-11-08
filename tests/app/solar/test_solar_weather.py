"""Provider-fixture tests for solar weather units, interval labels and optional archives."""

import asyncio
import json

import pytest

from open_energy_planner.solar.weather import FIELDS, OpenMeteoSolarSource
from tests.app.demand.test_demand_weather import Response
from tests.app.solar.support import SITE, START


class Session:
    """Capture bounded requests and return independent Open-Meteo-shaped responses without network."""

    def __init__(
        self,
        *,
        bad_unit=False,
        missing=False,
        archive_failure=False,
        oversized=False,
        overrides=None,
    ):
        """Select malformed weather or a failing optional archive independently of forecast availability."""
        self.calls = []
        self.bad_unit, self.missing = bad_unit, missing
        self.archive_failure, self.oversized = archive_failure, oversized
        self.overrides = overrides or {}

    def get(self, url, **kwargs):
        """Provide end-labelled hourly means. Missing radiation stays absent, not zero."""
        self.calls.append((url, kwargs))
        if self.archive_failure and "archive-api" in url:
            raise OSError("Optional archive unavailable")
        units = dict(zip(FIELDS, ("W/m²", "W/m²", "W/m²", "°C", "m/s"), strict=True))
        if self.bad_unit:
            units["wind_speed_10m"] = "km/h"
        values = dict(zip(FIELDS, (500, 600, 100, 15, 2), strict=True))
        values.update(self.overrides)
        hours = {key: [value, None if self.missing else value] for key, value in values.items()}
        hours["time"] = [START.timestamp() + 3600, START.timestamp() + 7200]
        raw = json.dumps({"hourly_units": units, "hourly": hours}).encode()
        return Response(b"x" * (4 * 1024 * 1024 + 1) if self.oversized else raw)


def test_solar_weather_end_labels_become_start_intervals_with_explicit_receipt():
    """An hour labelled 01 UTC represents [00,01), uses m/s, and preserves receipt time plus per-site daily caching."""

    async def scenario():
        """Exercise the production adapter against HTTP fixtures, never external weather services."""
        session = Session(missing=True)
        source = OpenMeteoSolarSource(session)
        values = await source.read_observed(SITE, START)
        assert len(values) == 1
        assert values[0].start == START
        assert values[0].received_at == START
        assert values[0].ghi == 500
        assert values[0].wind_ms == 2
        await source.read_observed(SITE, START)
        assert len(session.calls) == 3
        assert session.calls[0][1]["params"]["forecast_days"] == 4
        assert session.calls[0][1]["params"]["wind_speed_unit"] == "ms"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "attribute"),
    [
        ("shortwave_radiation", "ghi"),
        ("direct_normal_irradiance", "dni"),
        ("diffuse_radiation", "dhi"),
    ],
)
@pytest.mark.parametrize("negative", [-1.0, -(10**309)], ids=["negative_float", "large_integer"])
def test_negative_provider_radiation_is_zero_without_losing_weather_hours(
    field, attribute, negative
):
    """A provider's -1 W/m² becomes zero in both hours while -5 °C and wind remain unchanged."""

    async def scenario():
        """Read normal forecast/archive responses with one negative irradiance component."""
        session = Session(overrides={field: negative, "temperature_2m": -5})
        values = await OpenMeteoSolarSource(session).read_observed(SITE, START)
        assert len(values) == 2
        for weather in values:
            assert getattr(weather, attribute) == 0
            assert weather.celsius == -5
            assert weather.wind_ms == 2
            for unchanged, expected in (("ghi", 500), ("dni", 600), ("dhi", 100)):
                if unchanged != attribute:
                    assert getattr(weather, unchanged) == expected

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field", ["shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation"]
)
@pytest.mark.parametrize(
    "invalid",
    [
        float("-inf"),
        float("inf"),
        float("nan"),
        True,
        False,
        "-1",
        2001,
        pytest.param(10**309, id="large_integer"),
    ],
)
def test_radiation_normalization_does_not_accept_invalid_provider_values(field, invalid):
    """Nonfinite, boolean, textual and excessive irradiance must still fail provider validation."""

    async def scenario():
        """Keep malformed current weather distinct from valid zero radiation."""
        with pytest.raises(ValueError, match="Invalid solar quantity"):
            await OpenMeteoSolarSource(Session(overrides={field: invalid})).read_observed(
                SITE, START
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field", ["shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation"]
)
def test_missing_radiation_is_not_normalized_to_zero(field):
    """A missing irradiance component keeps its entire hour absent instead of inventing zero production."""

    async def scenario():
        """Read responses whose every hour lacks the selected radiation component."""
        assert (
            await OpenMeteoSolarSource(Session(overrides={field: None})).read_observed(SITE, START)
            == ()
        )

    asyncio.run(scenario())


def test_negative_provider_wind_is_not_normalized_to_zero():
    """A negative wind speed remains invalid because only irradiance has a zero-floor policy."""

    async def scenario():
        """Reject a malformed non-radiation field at the existing model boundary."""
        with pytest.raises(ValueError, match="Invalid solar quantity"):
            await OpenMeteoSolarSource(Session(overrides={"wind_speed_10m": -1})).read_observed(
                SITE, START
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("options", [{"bad_unit": True}, {"oversized": True}])
def test_solar_weather_rejects_unsupported_units_and_oversized_bodies(options):
    """Wrong wind units or more than 4 MiB cannot silently enter the local predictor."""

    async def scenario():
        """Reject malformed input before any yield calculation."""
        with pytest.raises(ValueError):
            await OpenMeteoSolarSource(Session(**options)).read_observed(SITE, START)

    asyncio.run(scenario())


def test_optional_historical_weather_failure_does_not_discard_valid_forecast():
    """An unavailable archive leaves two valid future hours usable for startup prediction."""

    async def scenario():
        """Keep the optional training-source outage separate from the current forecast source."""
        result = await OpenMeteoSolarSource(Session(archive_failure=True)).read_observed(
            SITE, START
        )
        assert len(result) == 2

    asyncio.run(scenario())


def test_excessive_integer_in_optional_archive_preserves_current_weather():
    """An archive integer beyond float range is rejected without discarding valid forecast hours."""

    class ExcessiveArchiveSession(Session):
        """Supply valid current radiation and an excessive optional archive value."""

        def get(self, url, **kwargs):
            """Corrupt only the archive with a finite integer beyond floating-point range."""
            self.overrides = {"shortwave_radiation": 10**309} if "archive-api" in url else {}
            return super().get(url, **kwargs)

    async def scenario():
        """Optional malformed history leaves independently valid current weather available."""
        values = await OpenMeteoSolarSource(ExcessiveArchiveSession()).read_observed(SITE, START)
        assert len(values) == 2
        assert all(weather.ghi == 500 for weather in values)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "malformed",
    [
        [],
        None,
        "unexpected",
        {"hourly_units": []},
        {"hourly": []},
        "missing_time",
        "missing_series",
        "overflow_time",
        "nonfinite_time",
    ],
)
def test_malformed_archive_shapes_preserve_current_forecast_but_current_failures_raise(malformed):
    """Malformed objects, missing series and invalid epochs reject current weather but cannot discard valid weather during optional archive failure."""

    class MalformedSession(Session):
        """Corrupt either the forecast or only the optional archive without external HTTP traffic."""

        def __init__(self, archive_only):
            """Select which endpoint receives the independently malformed response."""
            super().__init__()
            self.archive_only = archive_only

        def get(self, url, **kwargs):
            """Return valid current weather unless this request is the chosen corruption target."""
            valid = super().get(url, **kwargs)
            if self.archive_only and "archive-api" not in url:
                return valid
            units = dict(zip(FIELDS, ("W/m²", "W/m²", "W/m²", "°C", "m/s"), strict=True))
            hours = {key: [1] for key in FIELDS}
            hours["time"] = [START.timestamp() + 3600]
            if malformed == "missing_time":
                del hours["time"]
            elif malformed == "missing_series":
                del hours[FIELDS[0]]
            elif malformed == "overflow_time":
                hours["time"] = [1e100]
            elif malformed == "nonfinite_time":
                hours["time"] = [float("inf")]
            else:
                return Response(json.dumps(malformed).encode())
            return Response(json.dumps({"hourly_units": units, "hourly": hours}).encode())

    async def scenario():
        """Assert both strict provider-boundary errors and independent optional-source availability."""
        with pytest.raises(ValueError):
            await OpenMeteoSolarSource(MalformedSession(False)).read_observed(SITE, START)
        result = await OpenMeteoSolarSource(MalformedSession(True)).read_observed(SITE, START)
        assert len(result) == 2
        assert result[0].start == START

    asyncio.run(scenario())
