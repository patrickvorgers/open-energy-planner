"""Real local HTTP and planning tests for the demand forecast input boundary."""

import asyncio
from datetime import datetime, timedelta

from aiohttp import ClientSession

from tests.app.contract.support.settings import settings
from tests.app.forecast.support.http import configure_forecasts, running_forecasts
from tests.database.support.archive import read_document


def definition(**changes):
    """Select one directly measured AC household counter and a startup estimate, without helpers."""
    return {
        "timezone": "Europe/Amsterdam",
        "annual_kwh": 3650,
        "terms": [{"entity_id": "sensor.house", "sign": 1}],
        **changes,
    }


async def history_body(session, url, *, days=35, energy=0.4):
    """Read the current requested range and independently generate constant actual hourly energy."""
    async with session.get(url + "/v1/demand") as response:
        pending = (await response.json())["request"]
    end = datetime.fromisoformat(pending["end"])
    start = end - timedelta(days=days, hours=1)
    return {
        "request_version": pending["version"],
        "entity_id": pending["entity_id"],
        "unit": "kWh",
        "hours": [
            [
                (start + timedelta(hours=index)).timestamp(),
                100 + energy * index,
                100 + energy * index,
            ]
            for index in range(days * 24 + 1)
        ],
    }


async def wait_forecast(session, url, model):
    """Wait boundedly for the background model worker rather than fitting during a request."""
    for _ in range(150):
        async with session.get(url + "/v1/demand") as response:
            result = await response.json()
        if result["forecast"] and result["forecast"]["model"] == model:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError(result)


def test_demand_http_startup_history_and_real_plan_evidence(tmp_path):
    """Start without history, then learn 0.4 kWh/h and pass 0.1 kWh/quarter with exact forecast provenance into a solved plan."""

    async def scenario():
        """Use isolated forecasts/prices and real HTTP/HiGHS. No external weather or HA credentials are required."""
        async with running_forecasts(tmp_path) as (session, url):
            await configure_forecasts(session, url)
            async with session.put(url + "/v1/contracts", json=settings()) as response:
                assert response.status == 200
            async with session.put(url + "/v1/demand", json=definition()) as response:
                assert response.status == 200, await response.text()
                demand = await response.json()
            assert demand["forecast"]["model"] == "startup-v2"
            steps = demand["forecast"]["steps"]
            first = datetime.fromisoformat(steps[0]["start"])
            assert len(steps) == {0: 120, 15: 123, 30: 122, 45: 121}[first.minute]
            assert sum(step["duration_h"] for step in steps) == 48
            assert demand["forecast"]["quality"] == "provisional"
            original = tmp_path / "demand" / f"forecast-{demand['forecast']['version']}.json"
            saved = read_document(tmp_path, "demand", original.name)
            body = await history_body(session, url)
            async with session.post(url + "/v1/demand/history", json=body) as response:
                assert response.status == 200
            learned = await wait_forecast(session, url, "weekday-hour-v2")
            assert learned["candidate"]["model"] == "ridge-v2"
            assert learned["ridge_status"] == "shadow_waiting_for_decision_evidence"
            async with session.put(
                url + "/v1/demand",
                json=definition(expected_version=learned["configuration"]["version"]),
            ) as response:
                assert response.status == 200
                unchanged = await response.json()
            assert unchanged["forecast"] == learned["forecast"]
            assert unchanged["candidate"] == learned["candidate"]
            assert unchanged["request"] is None
            assert unchanged["ridge_status"] == learned["ridge_status"]
            # The learned source is coalesced. Request explicitly here to verify its planning provenance.
            async with session.post(url + "/v1/replan") as response:
                assert response.status == 200
            for _ in range(100):
                async with session.get(url + "/v1/snapshot") as response:
                    state = await response.json()
                plan = state.get("plan")
                if (
                    plan
                    and plan["input_snapshot"].get("demand_evidence")
                    and plan["input_snapshot"]["demand_evidence"][0]["forecast_version"]
                    == learned["forecast"]["version"]
                ):
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError(state["planning"])
            for step in plan["input_snapshot"]["problem"]["intervals"]:
                assert abs(step["load_kwh"] - 0.4 * step["duration_h"]) < 1e-7
            assert read_document(tmp_path, "demand", original.name) == saved
            async with session.put(url + "/v1/demand", json=definition()) as response:
                assert response.status == 409
            async with ClientSession() as anonymous, anonymous.get(url + "/v1/demand") as response:
                assert response.status == 401

    asyncio.run(scenario())


def test_demand_configuration_without_contract_retains_forecast_but_blocks_planning(tmp_path):
    """A current installation retains startup evidence without a tariff, but never publishes placeholder-priced commands."""

    async def scenario():
        """Use the actual installation compiler and authenticated HTTP without injecting a future-input substitute."""
        async with running_forecasts(tmp_path) as (session, url):
            await configure_forecasts(session, url)
            async with session.put(url + "/v1/demand", json=definition()) as response:
                assert response.status == 200, await response.text()
                demand = await response.json()
            assert demand["forecast"]["model"] == "startup-v2"
            async with session.get(url + "/v1/snapshot") as response:
                state = await response.json()
            assert state["plan"] is None
            assert state["planning"]["reason"] == "contract_not_configured"
            async with session.get(url + "/v1/contracts") as response:
                assert (await response.json())["status"] == "not_configured"
            async with session.get(url + "/v1/demand") as response:
                assert (await response.json())["forecast"] == demand["forecast"]

    asyncio.run(scenario())


def test_missing_exclusion_history_keeps_startup_visible_without_double_counting_plans(tmp_path):
    """Always predict total consumption when EV history is unknown, but refuse residual planning until separate scheduling exists."""

    async def scenario():
        """Exercise explicit scope/quality and later measured residuals without inventing flexible-device control."""
        async with running_forecasts(tmp_path) as (session, url):
            await configure_forecasts(session, url)
            async with session.put(url + "/v1/contracts", json=settings()) as response:
                assert response.status == 200
            async with session.put(
                url + "/v1/demand",
                json=definition(exclusions=[{"device_id": "ev", "entity_id": "sensor.ev"}]),
            ) as response:
                demand = await response.json()
                assert response.status == 200
            assert demand["forecast"]["model"] == "startup-v2"
            assert demand["forecast"]["scope"] == "total_household"
            assert demand["forecast"]["excluded_devices"] == []
            async with session.get(url + "/v1/snapshot") as response:
                assert (await response.json())["planning"][
                    "reason"
                ] == "separate_load_planning_not_configured"
            for energy in (0.6, 0.2):
                body = await history_body(session, url, days=7, energy=energy)
                async with session.post(url + "/v1/demand/history", json=body) as response:
                    assert response.status == 200
            demand = await wait_forecast(session, url, "weekday-hour-v2")
            assert demand["forecast"]["scope"] == "residual_household"
            assert demand["forecast"]["excluded_devices"] == ["ev"]
            assert abs(demand["forecast"]["steps"][0]["kwh"] - 0.1) < 1e-7
            async with session.get(url + "/v1/snapshot") as response:
                state = await response.json()
            assert state["plan"] is None
            assert state["planning"]["reason"] == "separate_load_planning_not_configured"

    asyncio.run(scenario())
