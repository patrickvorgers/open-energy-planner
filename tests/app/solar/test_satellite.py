"""Independent satellite wire fixtures preserve physical units, gaps and completed UTC hours."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.forecast_quality.daily import day_window
from open_energy_planner.forecast_quality.radiation import radiation_view
from open_energy_planner.solar.satellite import OpenMeteoSatelliteSource
from tests.app.demand.test_demand_weather import Response

START = datetime(2026, 10, 26, tzinfo=UTC)


class Session:
    """Return one bounded end-labelled response and retain the actual requested dataset."""

    def __init__(self, body):
        """Use independently specified wire data, without external weather requests."""
        self.body, self.calls = body, []

    def get(self, url, **kwargs):
        """Capture adapter policy while delivering chunked JSON through the existing HTTP fixture."""
        self.calls.append((url, kwargs))
        return Response(json.dumps(self.body).encode())


def payload(values):
    """Label each irradiance mean by its hour end, including a real nighttime zero."""
    return {
        "hourly_units": {"time": "unixtime", "shortwave_radiation": "W/m²"},
        "hourly": {
            "time": [
                (START + timedelta(hours=index + 1)).timestamp() for index in range(len(values))
            ],
            "shortwave_radiation": values,
        },
    }


def test_satellite_uses_named_dataset_and_retains_zero_gaps_and_completion():
    """Only hours 00 and 02 survive a missing hour and a future fourth hour."""
    session = Session(payload([0, None, 500, 600]))
    result = asyncio.run(
        OpenMeteoSatelliteSource(session).read(
            {"latitude": 52.52, "longitude": 13.41},
            START,
            START + timedelta(days=1),
            START + timedelta(hours=3, minutes=20),
        )
    )
    assert [(row.start, row.ghi) for row in result] == [
        (START, 0),
        (START + timedelta(hours=2), 500),
    ]
    url, request = session.calls[0]
    assert url == "https://satellite-api.open-meteo.com/v1/archive"
    assert request["params"]["models"] == "dwd_sis_europe_africa_v4"
    assert request["params"]["end_date"] == "2026-10-27"
    assert request["allow_redirects"] is False


@pytest.mark.parametrize("invalid", [True, "500", -1, 2001, float("nan"), float("inf"), 10**309])
def test_satellite_rejects_invalid_irradiance(invalid):
    """Invalid sensor-like values never become usable satellite evidence or zero."""
    with pytest.raises((ValueError, TypeError, OverflowError)):
        asyncio.run(
            OpenMeteoSatelliteSource(Session(payload([invalid]))).read(
                {"latitude": 52, "longitude": 5},
                START,
                START + timedelta(days=1),
                START + timedelta(days=2),
            )
        )


@pytest.mark.parametrize(
    "defect", ["units", "duplicate", "fractional", "length", "oversized", "shape"]
)
def test_satellite_rejects_malformed_units_times_and_shapes(defect):
    """Malformed external data fails before any observation can enter the archive."""
    body = payload([1, 2])
    if defect == "units":
        body["hourly_units"]["shortwave_radiation"] = "J/cm²"
    elif defect == "duplicate":
        body["hourly"]["time"][1] = body["hourly"]["time"][0]
    elif defect == "fractional":
        body["hourly"]["time"][0] += 30
    elif defect == "length":
        body["hourly"]["time"].pop()
    elif defect == "oversized":
        body = payload([1] * 73)
    else:
        body = []
    with pytest.raises((ValueError, TypeError)):
        asyncio.run(
            OpenMeteoSatelliteSource(Session(body)).read(
                {"latitude": 52, "longitude": 5},
                START,
                START + timedelta(days=1),
                START + timedelta(days=2),
            )
        )


@pytest.mark.parametrize(
    ("day", "hours"), [("2026-03-29", 23), ("2026-10-25", 25), ("2026-10-31", 24)]
)
def test_coverage_uses_real_day_edges_and_stops_at_first_gap(day, hours):
    """A gap after two complete hours stops the marker even when later hours cross DST or month boundaries."""
    start, end = day_window(day, "Europe/Amsterdam")
    satellite = {
        "hours": [
            {"start": (start + timedelta(hours=index)).isoformat(), "ghi": 0 if index == 0 else 300}
            for index in range(hours)
            if index != 2
        ]
    }
    view = radiation_view({}, satellite, start, end, end + timedelta(hours=1))
    assert view["complete_until"] == (start + timedelta(hours=2)).isoformat()
    assert view["observed_hours"] == hours - 1
    assert view["hours"] == hours
    assert view["rows"][0]["satellite_w_m2"] == 0
    assert view["rows"][2]["satellite_w_m2"] is None
    assert view["rows"][-1]["end"] == end.isoformat()


def test_empty_future_and_fractional_boundary_do_not_claim_complete_coverage():
    """Unknown, future and fractional edge evidence cannot establish a complete-through marker."""
    for start, now in [(START, START), (START + timedelta(minutes=30), START + timedelta(days=1))]:
        end = start + timedelta(days=1)
        view = radiation_view(
            {}, {"hours": [{"start": START.isoformat(), "ghi": 500}]}, start, end, now
        )
        assert view["complete_until"] is None
