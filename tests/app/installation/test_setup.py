"""Verify annual household estimates, first-hour setup windows and optional export-sensor preview validation."""

import asyncio
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.installation.calculation.annual_estimate import annual_estimate
from tests.ha_contract.support.metadata import setup_metadata


@pytest.mark.parametrize("available_days", [365, 180, 28])
def test_setup_uses_all_available_days_in_the_last_year(available_days):
    """A recent winter peak must not replace the available year's seasonal balance."""
    end_day = 20000
    cumulative_kwh = 100.0
    points = [[(end_day - available_days) * 86400, cumulative_kwh]]
    for day_index in range(available_days):
        cumulative_kwh += 20 if day_index >= available_days - 28 else 5
        points.append([(end_day - available_days + day_index + 1) * 86400, cumulative_kwh])
    estimate = annual_estimate(
        [{"entity_id": "sensor.import", "sign": 1}],
        [{"entity_id": "sensor.import", "points": [points[0], points[-1]]}],
        now=datetime.fromtimestamp(end_day * 86400, UTC),
    )
    expected_consumption = (available_days - 28) * 5 + 28 * 20
    assert estimate == {
        "annual_kwh": round(expected_consumption / available_days * 365, 1),
        "days": available_days,
        "start": (end_day - available_days) * 86400,
        "end": end_day * 86400,
        "extrapolated": available_days < 365,
    }


def test_setup_year_uses_net_sum_corrections_without_daily_records():
    """A counter rising from 100 to 300 then corrected to 250 contributes 150 kWh over the year."""
    history = [[20000 * 86400, 100], [20100 * 86400, 300], [20365 * 86400, 250]]
    estimate = annual_estimate(
        [{"entity_id": "sensor.import", "sign": 1}],
        [{"entity_id": "sensor.import", "points": [history[0], history[-1]]}],
        now=datetime.fromtimestamp(20365 * 86400, UTC),
    )
    assert estimate["days"] == 365
    assert estimate["annual_kwh"] == 150
    assert estimate["extrapolated"] is False


@pytest.mark.parametrize("days_after_end,available", [(0, True), (1, True), (2, False)])
def test_setup_year_survives_one_midnight_while_catalogue_refreshes(days_after_end, available):
    """An unchanged full-year snapshot survives one UTC rollover without accepting indefinitely old references."""
    estimate = annual_estimate(
        [{"entity_id": "sensor.import", "sign": 1}],
        [{"entity_id": "sensor.import", "points": [[20000 * 86400, 100], [20365 * 86400, 3750]]}],
        now=datetime.fromtimestamp((20365 + days_after_end) * 86400 + 60, UTC),
    )
    if available:
        assert estimate["days"] == 365
        assert estimate["annual_kwh"] == 3650
        assert estimate["extrapolated"] is False
    else:
        assert estimate is None


def test_setup_rollover_grace_does_not_allow_a_longer_consumption_period():
    """A grace day on freshness does not admit a 366-day consumption window."""
    with pytest.raises(ValueError, match="exceeds one year"):
        annual_estimate(
            [{"entity_id": "sensor.import", "sign": 1}],
            [
                {
                    "entity_id": "sensor.import",
                    "points": [[20000 * 86400, 100], [20366 * 86400, 3760]],
                }
            ],
            now=datetime.fromtimestamp(20366 * 86400, UTC),
        )


def test_setup_does_not_combine_meters_with_different_reference_periods():
    """A year of import cannot be combined with solar whose reference starts a day later."""
    assert (
        annual_estimate(
            [{"entity_id": "sensor.import", "sign": 1}, {"entity_id": "sensor.pv", "sign": 1}],
            [
                {
                    "entity_id": "sensor.import",
                    "points": [[20000 * 86400, 100], [20365 * 86400, 3750]],
                },
                {"entity_id": "sensor.pv", "points": [[20001 * 86400, 0], [20365 * 86400, 720]]},
            ],
            now=datetime.fromtimestamp(20365 * 86400, UTC),
        )
        is None
    )


@pytest.mark.parametrize("first,last", [(100, 99), (-1e308, 1e308), (0, 1e308)])
def test_setup_invalid_period_differences_do_not_create_an_annual_scale(first, last):
    """A negative net difference or floating-point overflow remains unavailable instead of inventing consumption."""
    assert (
        annual_estimate(
            [{"entity_id": "sensor.import", "sign": 1}],
            [
                {
                    "entity_id": "sensor.import",
                    "points": [[20000 * 86400, first], [20007 * 86400, last]],
                }
            ],
            now=datetime.fromtimestamp(20007 * 86400, UTC),
        )
        is None
    )


def test_setup_annual_estimate_uses_household_balance_and_common_days():
    """Import 10 + PV 6 + discharge 2 - export 3 - charge 4 = 11 kWh/day, implying 4015 kWh/year over one common period."""
    terms = [
        {"entity_id": "sensor." + key, "sign": sign}
        for key, sign in (
            ("import", 1),
            ("pv", 1),
            ("discharge", 1),
            ("export", -1),
            ("charge", -1),
        )
    ]
    history = [
        {
            "entity_id": energy_term["entity_id"],
            "points": [[20000 * 86400, 0], [20008 * 86400, 8 * amount]],
        }
        for energy_term, amount in zip(terms, (10, 6, 2, 3, 4), strict=True)
    ]
    assert (
        annual_estimate(terms, history, now=datetime.fromtimestamp(20008 * 86400, UTC))[
            "annual_kwh"
        ]
        == 4015
    )
    history[-1]["points"][0][0] += 86400
    assert (
        annual_estimate(terms, history, now=datetime.fromtimestamp(20008 * 86400, UTC)) is None
    )  # Every channel must describe the same period.
    assert (
        annual_estimate(terms, history[:-1], now=datetime.fromtimestamp(20008 * 86400, UTC)) is None
    )
    history[0]["points"][0][1] = float("nan")
    with pytest.raises(ValueError, match="endpoint"):
        annual_estimate(terms, history, now=datetime.fromtimestamp(20008 * 86400, UTC))


@pytest.mark.parametrize("hour,endpoint_count,expected_days", [(0, 28, 27), (2, 29, 28)])
def test_setup_estimate_before_and_after_first_utc_hour(hour, endpoint_count, expected_days):
    """An unfinished midnight statistics row shortens the setup window without inventing a day or changing its mean."""
    now = datetime.fromtimestamp(20028 * 86400 + hour * 3600 + 1800, UTC)
    history = [
        {
            "entity_id": "sensor.import",
            "points": [
                [(20000 + index) * 86400, 100 + index * 10] for index in (0, endpoint_count - 1)
            ],
        }
    ]
    estimate = annual_estimate([{"entity_id": "sensor.import", "sign": 1}], history, now=now)
    assert estimate == {
        "annual_kwh": 3650,
        "days": expected_days,
        "start": 20000 * 86400,
        "end": (20000 + expected_days) * 86400,
        "extrapolated": True,
    }


@pytest.mark.parametrize("kind", ["missing", "power", "no_sum", "energy"])
def test_setup_preview_validates_optional_export_sensor(tmp_path, kind):
    """Unknown, power and non-cumulative export sensors are rejected before saving. A cumulative Wh meter is accepted."""

    async def scenario():
        """Call the authenticated preview directly so browser dropdown filtering cannot hide a missing server check."""
        async with TestClient(
            TestServer(create_app(tmp_path, "setup-test-token-0123456789", source="home_assistant"))
        ) as client:
            headers = {"Authorization": "Bearer setup-test-token-0123456789"}
            catalogue = setup_metadata()
            catalogue["suggestions"]["grid_export"] = "sensor.export"
            catalogue["suggestions"]["terms"].append({"entity_id": "sensor.export", "sign": -1})
            if kind != "missing":
                catalogue["entities"].append(
                    {
                        "entity_id": "sensor.export",
                        "name": "Export",
                        "unit": "W" if kind == "power" else "Wh",
                        "device_id": "meter",
                        "device_class": "power" if kind == "power" else "energy",
                        "state_class": "total_increasing" if kind == "energy" else "measurement",
                    }
                )
            assert (
                await client.post("/v1/ha/catalogue", headers=headers, json=catalogue)
            ).status == 200
            body = {
                "installation": {
                    "grid_power_entity": "sensor.grid",
                    "grid_export_entity": "sensor.export",
                    "import_limit_w": 5000,
                    "export_limit_w": 5000,
                },
                "solar": {"sources": [], "inverters": []},
                "demand": {
                    "annual_kwh": 3500,
                    "timezone": "Europe/Amsterdam",
                    "terms": catalogue["suggestions"]["terms"],
                    "exclusions": [],
                },
            }
            response = await client.post("/v1/setup/preview", headers=headers, json=body)
            assert response.status == (200 if kind == "energy" else 400)
            state = await (await client.get("/v1/setup", headers=headers)).json()
            assert state["installation"] is None

    asyncio.run(scenario())
