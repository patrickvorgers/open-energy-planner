"""Authenticated solar configuration, weather and real-solver planning integration."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import ClientSession

from open_energy_planner.solar.wire import configuration_document
from tests.app.contract.support.settings import settings
from tests.app.demand.test_demand_app import definition
from tests.app.forecast.support.http import configure_forecasts, running_forecasts
from tests.app.planning.test_live_app import wait_ready
from tests.app.solar.support import fixture_weather, plant
from tests.database.support.archive import read_document


@pytest.mark.parametrize("large", [False, True])
def test_solar_feedback_http_binds_sensor_configuration_and_checked_cursor(tmp_path, large):
    """A normalized response advances only its current configured sensor. Stale, foreign and unauthorized writes preserve it."""
    from dataclasses import replace

    async def scenario():
        """Exercise the real authenticated route and durable current-feedback pointer."""
        async with running_forecasts(tmp_path, weather_factory=fixture_weather) as (session, url):
            configuration = plant()
            configuration = replace(
                configuration,
                sources=(
                    replace(
                        configuration.sources[0],
                        curtailment_entity_id="binary_sensor.synthetic_curtailment",
                    ),
                ),
            )
            definition = configuration_document(configuration)
            definition.pop("version")
            configured = await configure_forecasts(session, url, solar_definition=definition)
            request = configured["solar"]["feedback_requests"][0]
            body = {
                "configuration_version": configuration.version,
                "start": request["start"],
                "feedback": {
                    "entity_id": request["entity_id"],
                    "checked_until": request["end"],
                    "readings": [],
                },
            }
            if large:
                import json

                first = datetime.fromisoformat(request["start"])
                last = datetime.fromisoformat(request["end"])
                body["feedback"]["readings"] = [
                    [(first + timedelta(hours=index)).isoformat(), bool(index % 2)]
                    for index in range(int((last - first).total_seconds() // 3600))
                ]
                assert 256 * 1024 < len(json.dumps(body).encode()) < 1024 * 1024
            async with session.post(url + "/v1/solar/feedback", json=body) as response:
                assert response.status == 200
                accepted = await response.json()
            assert accepted["feedback_requests"][0]["start"] == request["end"]
            async with session.post(url + "/v1/solar/feedback", json=body) as response:
                assert response.status == 400
            foreign = {**body, "configuration_version": "f" * 64}
            async with session.post(url + "/v1/solar/feedback", json=foreign) as response:
                assert response.status == 400
            async with (
                ClientSession() as untrusted,
                untrusted.post(url + "/v1/solar/feedback", json=body) as response,
            ):
                assert response.status == 401
            async with session.get(url + "/v1/solar") as response:
                current = await response.json()
            assert current["feedback_requests"][0]["start"] == request["end"]
            assert current["configuration"]["version"] == configuration.version

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["write", "projection", "projection_lookalike"])
def test_feedback_failure_keeps_private_values_out_of_response_and_logs(
    tmp_path, monkeypatch, caplog, phase
):
    """Unexpected storage and post-commit projection failures retain durable state and expose only owned recovery advice."""
    from dataclasses import replace

    from open_energy_planner.solar.runtime import SolarRuntime

    async def scenario():
        """Fail the actual HTTP operation before or after its durable write without echoing exception values."""
        async with running_forecasts(tmp_path, weather_factory=fixture_weather) as (session, url):
            configuration = plant()
            configuration = replace(
                configuration,
                sources=(
                    replace(
                        configuration.sources[0],
                        curtailment_entity_id="binary_sensor.synthetic_curtailment",
                    ),
                ),
            )
            definition = configuration_document(configuration)
            definition.pop("version")
            configured = await configure_forecasts(session, url, solar_definition=definition)
            request = configured["solar"]["feedback_requests"][0]
            body = {
                "configuration_version": configuration.version,
                "start": request["start"],
                "feedback": {
                    "entity_id": request["entity_id"],
                    "checked_until": request["end"],
                    "readings": [],
                },
            }
            original_receive, original_document = (
                SolarRuntime.receive_feedback,
                SolarRuntime.document,
            )
            committed = False

            def receive(runtime, body, now):
                """Inject a driver failure or finish the real transaction before making projection fail."""
                nonlocal committed
                if phase == "write":
                    raise OSError("private-feedback-storage-sentinel")
                original_receive(runtime, body, now)
                committed = True

            def project(runtime, now, **options):
                """Raise only after feedback acceptance to prove that durable writes are not called invalid input."""
                if committed:
                    if phase == "projection_lookalike":
                        from open_energy_planner.solar.curtailment import FeedbackRequestError

                        raise FeedbackRequestError("private-feedback-lookalike-sentinel")
                    raise ValueError("private-feedback-projection-sentinel")
                return original_document(runtime, now, **options)

            with monkeypatch.context() as failure:
                failure.setattr(SolarRuntime, "receive_feedback", receive)
                failure.setattr(SolarRuntime, "document", project)
                async with session.post(url + "/v1/solar/feedback", json=body) as response:
                    assert response.status == 503
                    message = await response.text()
                    assert "private-feedback" not in message
            async with session.get(url + "/v1/solar") as response:
                restored = await response.json()
            assert (
                restored["feedback_requests"][0]["start"]
                == request["start" if phase == "write" else "end"]
            )

    asyncio.run(scenario())
    assert "private-feedback" not in caplog.text


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


def test_solar_history_api_discovers_years_and_commits_empty_and_gap_preserving_chunks(
    tmp_path, monkeypatch
):
    """Authenticated availability and annual reports persist bounded imports without filling absent measurements."""
    from open_energy_planner import app as app_module
    from open_energy_planner.solar import api as solar_api

    frozen = datetime.now(UTC)

    class Clock(datetime):
        """Keep the API and snapshot on one day even if a slow runner crosses UTC midnight."""

        @classmethod
        def now(cls, timezone=None):
            """Return the same aware instant while preserving the standard naive-call behavior."""
            return frozen.astimezone(timezone) if timezone else frozen.replace(tzinfo=None)

    monkeypatch.setattr(app_module, "datetime", Clock)
    monkeypatch.setattr(solar_api, "datetime", Clock)

    async def scenario():
        """Exercise the real route, current request identity and active database on synthetic solar history."""
        async with running_forecasts(tmp_path, weather_factory=fixture_weather) as (session, url):
            definition = configuration_document(plant())
            definition.pop("version")
            await configure_forecasts(session, url, solar_definition=definition)
            async with session.get(url + "/v1/solar") as response:
                pending = (await response.json())["request"]
            target_year = datetime.fromisoformat(pending["end"]).year
            first_year = target_year - 1
            identity = {"metadata_id": 33, "source": "recorder", "unit": "kWh"}
            assert pending["basis"] == "solar_history_range"
            async with session.post(
                url + "/v1/solar/history",
                json={
                    "request_version": pending["version"],
                    "basis": "solar_history_range",
                    "entity_id": "sensor.pv0",
                    "statistics_identity": identity,
                    "available_start": f"{first_year}-01-01T00:00:00+00:00",
                },
            ) as response:
                assert response.status == 200
                pending = (await response.json())["solar"]["request"]
            assert pending["import_year"] == first_year
            async with session.post(
                url + "/v1/solar/history",
                json={
                    "request_version": pending["version"],
                    "basis": "solar_history_chunk",
                    "entity_id": "sensor.pv0",
                    "statistics_identity": identity,
                    "unit": "kWh",
                    "hours": [],
                },
            ) as response:
                assert response.status == 200
                pending = (await response.json())["solar"]["request"]
            assert pending["import_year"] == target_year
            first = datetime.fromisoformat(pending["start"])
            body = {
                "request_version": pending["version"],
                "basis": "solar_history_chunk",
                "entity_id": "sensor.pv0",
                "statistics_identity": identity,
                "unit": "kWh",
                "hours": [
                    [(first + timedelta(hours=index)).timestamp(), 100 + index, 100 + index]
                    for index in (0, 1, 3)
                ],
            }
            async with session.post(
                url + "/v1/solar/history", json={**body, "unit": "unsupported"}
            ) as response:
                assert response.status == 400
            async with session.post(url + "/v1/solar/history", json=body) as response:
                assert response.status == 200
                state = (await response.json())["solar"]
            assert state["request"] is None
            assert state["history_import"]["sensor.pv0"]["loaded_years"] == [
                first_year,
                target_year,
            ]
            assert state["history_import"]["sensor.pv0"]["hours"] == 3
            async with session.post(url + "/v1/solar/history", json=body) as response:
                assert response.status == 409

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


def test_learning_start_is_saved_with_cas_and_rejects_future_and_malformed_dates(tmp_path):
    """An explicit learning boundary survives reload and invalid edits cannot overwrite the saved configuration."""

    async def scenario():
        """Exercise the authenticated configuration route using independent past and future instants."""
        async with running_forecasts(tmp_path, weather_factory=fixture_weather) as (session, url):
            initial = configuration_document(plant())
            initial.pop("version")
            configured = await configure_forecasts(session, url, solar_definition=initial)
            current = configured["solar"]["configuration"]
            cutoff = datetime.now(UTC).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=30)
            body = {
                **current,
                "expected_version": current["version"],
                "sources": [{**current["sources"][0], "learning_start": cutoff.isoformat()}],
            }
            async with session.put(url + "/v1/solar", json=body) as response:
                assert response.status == 200
                saved = (await response.json())["configuration"]
            assert saved["sources"][0]["learning_start"] == cutoff.isoformat()
            assert saved["version"] != current["version"]
            async with session.put(url + "/v1/solar", json=body) as response:
                assert response.status == 409
            for invalid in (
                "invalid",
                "2026-04-01",
                (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            ):
                rejected = {
                    **body,
                    "expected_version": saved["version"],
                    "sources": [{**body["sources"][0], "learning_start": invalid}],
                }
                async with session.put(url + "/v1/solar", json=rejected) as response:
                    assert response.status == 400
            async with session.get(url + "/v1/solar") as response:
                assert (await response.json())["configuration"] == saved

    asyncio.run(scenario())
