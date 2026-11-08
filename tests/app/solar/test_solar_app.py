"""Authenticated solar configuration, weather and real-solver planning integration."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from aiohttp import ClientSession

from open_energy_planner.solar.wire import configuration_document
from tests.app.contract.support.settings import settings
from tests.app.demand.test_demand_app import definition
from tests.app.forecast.support.http import configure_forecasts, running_forecasts
from tests.app.planning.test_live_app import wait_ready
from tests.app.solar.test_solar import SITE, plant, weather
from tests.database.support.archive import read_document


@asynccontextmanager
async def fixture_weather():
    """Supply complete normalized solar weather locally, without provider URLs or production HA access."""

    class Source:
        """Implement the solar-weather port with deterministic physical inputs."""

        async def read(self, location, now):
            """Return weather at the actual requested site/time, keeping knowledge timestamps consistent."""
            assert location == SITE
            return weather(now)

    yield Source()


def test_solar_api_projects_two_sources_into_real_plan_and_retains_original_archive(tmp_path):
    """Configure two plants through HTTP. Preserve source DC evidence and apply each independent 96% converter exactly once."""

    async def scenario():
        """Use a declared installation fixture and weather port. Actual HTTP/auth/model/solver paths remain unchanged."""
        async with running_forecasts(tmp_path, weather_factory=fixture_weather) as (session, url):
            solar_definition = configuration_document(plant(count=2))
            solar_definition.pop("version")
            configured = await configure_forecasts(session, url, solar_definition=solar_definition)
            async with session.put(url + "/v1/contracts", json=settings()) as response:
                assert response.status == 200
            async with session.put(
                url + "/v1/demand",
                json=definition(
                    terms=[
                        {"entity_id": entity, "sign": 1}
                        for entity in ("sensor.house", "sensor.pv0", "sensor.pv1")
                    ]
                ),
            ) as response:
                assert response.status == 200
            solar_definition["expected_version"] = configured["solar"]["configuration"]["version"]
            async with session.put(url + "/v1/solar", json=solar_definition) as response:
                assert response.status == 200
                initial = await response.json()
            for _ in range(300):
                async with session.get(url + "/v1/solar?series=1") as response:
                    data = await response.json()
                if data["forecast"]:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError(data)
            forecast = data["forecast"]
            first = datetime.fromisoformat(forecast["energies"][0]["start"])
            assert (
                len(forecast["energies"]) == 2 * {0: 120, 15: 123, 30: 122, 45: 121}[first.minute]
            )
            assert forecast["model"] == "pvwatts-v2"
            published = (await wait_ready(session, url))["plan"]
            plan = published["input_snapshot"]
            assert plan["solar_evidence"][0]["source_ids"] == ["pv0", "pv1"]
            assert {item["inverter_id"] for item in plan["problem"]["inverters"]} == {"i0", "i1"}
            assert all(item["efficiency"] == 0.96 for item in plan["problem"]["inverters"])
            for step, flow in zip(
                plan["problem"]["intervals"], published["expected_flows"], strict=True
            ):
                start = datetime.fromisoformat(step["start"])
                end = start + timedelta(hours=step["duration_h"])
                expected = 0.0
                expected_sources = {"pv0": 0.0, "pv1": 0.0}
                for energy in forecast["energies"]:
                    left = datetime.fromisoformat(energy["start"])
                    overlap = (
                        min(end, left + timedelta(hours=energy["duration_h"])) - max(start, left)
                    ).total_seconds() / 3600
                    if overlap > 0:
                        source_dc_kwh = energy["dc_kwh"] * overlap / energy["duration_h"]
                        expected += source_dc_kwh
                        expected_sources[energy["source_id"]] += source_dc_kwh
                assert step["pv_kwh"] == pytest.approx(expected)
                assert sum(
                    inverter["export_kwh"] for inverter in flow["inverters"]
                ) == pytest.approx(expected * 0.96)
                assert dict(step["pv_sources"]) == pytest.approx(expected_sources)
                inverter_flows = {
                    inverter["inverter_id"]: inverter for inverter in flow["inverters"]
                }
                for inverter_id, source_id in (("i0", "pv0"), ("i1", "pv1")):
                    assert inverter_flows[inverter_id]["export_kwh"] == pytest.approx(
                        expected_sources[source_id] * 0.96
                    )
            archive = tmp_path / "solar" / f"forecast-{forecast['version']}.json"
            original = read_document(tmp_path, "solar", archive.name)
            solar_definition["expected_version"] = initial["configuration"]["version"]
            async with session.put(url + "/v1/solar", json=solar_definition) as response:
                assert response.status == 200
                assert (await response.json())["forecast"]["version"] == forecast["version"]
            async with session.put(
                url + "/v1/solar", json={**solar_definition, "expected_version": "stale"}
            ) as response:
                assert response.status == 409
            assert read_document(tmp_path, "solar", archive.name) == original
            pending = data["request"]
            async with session.post(
                url + "/v1/solar/history",
                json={"request_version": pending["version"], "error": "unsupported_statistics"},
            ) as response:
                assert response.status == 200
                state = await response.json()
            assert state["solar"]["request"]["entity_id"] == "sensor.pv1"
            async with session.post(
                url + "/v1/solar/history",
                json={"request_version": pending["version"], "error": "history_unavailable"},
            ) as response:
                assert response.status == 409
            async with (
                ClientSession() as anonymous,
                anonymous.get(url + "/v1/solar") as response,
            ):
                assert response.status == 401

    asyncio.run(scenario())


def test_unrestricted_history_confirmation_is_bounded_and_concurrency_protected(tmp_path):
    """Persist an explicit past declaration, rejecting future, partial and stale writes unchanged."""

    async def scenario():
        """Exercise the actual authenticated settings route with independent UTC period inputs."""
        async with running_forecasts(tmp_path, weather_factory=fixture_weather) as (session, url):
            initial = configuration_document(plant())
            initial.pop("version")
            configured = await configure_forecasts(session, url, solar_definition=initial)
            current = configured["solar"]["configuration"]
            body = {
                **current,
                "sources": [
                    {
                        **current["sources"][0],
                        "can_curtail": True,
                        "unrestricted_history_start": "2025-01-01T00:00:00+00:00",
                        "unrestricted_history_end": "2025-12-31T00:00:00+00:00",
                    }
                ],
                "expected_version": current["version"],
            }
            async with session.put(url + "/v1/solar", json=body) as response:
                assert response.status == 200
                saved = (await response.json())["configuration"]
            assert saved["sources"][0]["unrestricted_history_start"] == "2025-01-01T00:00:00+00:00"
            assert saved["sources"][0]["unrestricted_history_end"] == "2025-12-31T00:00:00+00:00"
            async with session.put(url + "/v1/solar", json=body) as response:
                assert response.status == 409
            for end in (None, "2099-01-01T00:00:00+00:00"):
                invalid = {
                    **body,
                    "expected_version": saved["version"],
                    "sources": [{**body["sources"][0], "unrestricted_history_end": end}],
                }
                async with session.put(url + "/v1/solar", json=invalid) as response:
                    assert response.status == 400
            async with session.get(url + "/v1/solar") as response:
                assert (await response.json())["configuration"] == saved

    asyncio.run(scenario())
