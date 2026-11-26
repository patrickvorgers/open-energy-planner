"""Keep source identities and actionable causes in the real planning notification projection."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from open_energy_planner.home_assistant.notifications import explain_measurement_problem
from open_energy_planner.runtime.diagnostics.planning_notifications import (
    PlanningStatus,
    planning_notifications,
)
from tests.app.notifications.support import measurement_evidence

NOW = datetime(2026, 11, 1, 9, tzinfo=UTC)


def test_real_authenticated_endpoint_prioritizes_one_sensor_and_recovers_from_restart(tmp_path):
    """A confirmed restart replaces a measurement warning, which returns if the sensor is still missing."""
    from dataclasses import asdict
    from types import SimpleNamespace

    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.app import create_app
    from open_energy_planner.home_assistant.observation_wire import configuration_document

    async def scenario():
        """Exercise actual source/observation/notification routes without bypassing their validation or authorization."""
        now = datetime.now(UTC)
        config, report = measurement_evidence(now)
        token = "measurement-notification-test-only"
        app = create_app(
            tmp_path,
            token,
            source="home_assistant",
            basis_source=SimpleNamespace(current=lambda *args, **kwargs: None),
        )
        headers = {"Authorization": "Bearer " + token}
        async with TestClient(TestServer(app), headers=headers) as client:
            response = await client.put("/v1/sources", json=configuration_document(config))
            assert response.status == 200
            current = await response.json()
            revision = current["measurement_config"]["revision"]
            response = await client.post(
                "/v1/observations",
                json={
                    "config_revision": revision,
                    "captured_at": now.isoformat(),
                    "readings": [],
                    "issues": [asdict(issue) for issue in report.issues],
                },
            )
            assert response.status == 200
            response = await client.get("/v1/notifications")
            assert response.status == 200
            notices = (await response.json())["notifications"]
            matching = [
                notice for notice in notices if notice["code"] == "required_measurement_unavailable"
            ]
            assert len(matching) == 1
            assert "sensor.grid: The configured sensor does not exist" in matching[0]["message"]
            assert "sensor.soc" not in matching[0]["message"]
            assert token not in str(notices)

            response = await client.post(
                "/v1/ha/lifecycle",
                json={"state": "restarting"},
                headers={"Authorization": "Bearer unrelated"},
            )
            assert response.status == 401
            response = await client.post("/v1/ha/lifecycle", json={"state": "unknown"})
            assert response.status == 400
            response = await client.post("/v1/ha/lifecycle", json={"state": "restarting"})
            assert response.status == 200
            response = await client.get("/v1/notifications")
            notices = (await response.json())["notifications"]
            assert not any(
                notice["code"] == "required_measurement_unavailable" for notice in notices
            )
            restart = next(
                notice for notice in notices if notice["code"] == "home_assistant_restarting"
            )
            assert restart["title"] == "Home Assistant is restarting"
            assert restart["level"] == "info"
            assert restart["blocks"] == []

            response = await client.post("/v1/ha/lifecycle", json={"state": "started"})
            assert response.status == 200
            response = await client.get("/v1/notifications")
            notices = (await response.json())["notifications"]
            assert not any(notice["code"] == "home_assistant_restarting" for notice in notices)
            assert any("sensor.grid" in notice["message"] for notice in notices)

    asyncio.run(scenario())


def test_first_required_sensor_and_its_cause_survive_planning_projection():
    """Show the first blocking sensor without listing other required and optional sources."""
    config, report = measurement_evidence(NOW)
    state = PlanningStatus("not_ready", "required_measurement_unavailable", NOW)
    notices = planning_notifications(
        state, lambda cause: explain_measurement_problem(cause, config, report, NOW)
    )
    assert len(notices) == 1
    message = notices[0].document("planning")["message"]
    assert "sensor.grid: The configured sensor does not exist" in message
    assert "sensor.soc" not in message
    assert "sensor.pv" not in message
    assert "No new energy plan can be published" in message


def test_current_energy_names_keep_sensor_ids_and_causes_in_both_notice_projections():
    """The shared banner/drawer notice includes current Energy names without hiding sensor identities or remedies."""
    config, report = measurement_evidence(NOW)
    names = {"sensor.grid": "Grid meter"}
    state = PlanningStatus("not_ready", "required_measurement_unavailable", NOW)
    notices = planning_notifications(
        state, lambda cause: explain_measurement_problem(cause, config, report, NOW, names)
    )
    message = notices[0].document("planning")["message"]
    assert "Grid meter (sensor.grid): The configured sensor does not exist" in message
    assert "sensor.soc" not in message


def test_expired_bridge_names_integration_instead_of_claiming_sensor_failure():
    """A five-minute capture outage explains the transport deadline without blaming unchanged sensor timestamps."""
    config, report = measurement_evidence(NOW)
    text = explain_measurement_problem("report_expired", config, report, NOW + timedelta(minutes=5))
    assert "Open Energy Planner integration" in text.message
    assert "5 minutes" in text.message
    assert "not the age of an unchanged sensor value" in text.message


def test_unknown_source_issue_never_echoes_private_diagnostic_payload():
    """Unrecognized integration reasons retain the configured identity but cannot leak raw provider text."""
    config, report = measurement_evidence(NOW)
    report = replace(
        report, issues=(replace(report.issues[0], reason="secret-test-token"), *report.issues[1:])
    )
    message = explain_measurement_problem(
        "required_measurement_unavailable", config, report, NOW
    ).message
    assert "sensor.grid" in message
    assert "secret-test-token" not in message


def test_recovered_measurements_keep_the_recorded_sensor_until_planning_recovers():
    """A refreshed measurement report must not replace known evidence with a generic warning."""
    config, report = measurement_evidence(NOW)
    explanation = explain_measurement_problem(
        "required_measurement_unavailable", config, report, NOW
    )
    state = PlanningStatus(
        "not_ready", "required_measurement_unavailable", NOW, block_text=explanation
    )
    [blocked] = planning_notifications(state, lambda _reason: None)
    assert "sensor.grid: The configured sensor does not exist" in blocked.text.message
    assert "sensor.soc" not in blocked.text.message
    recovering = replace(state, status="calculating", block_reason=state.reason, reason=None)
    [calculating] = planning_notifications(recovering, lambda _reason: None)
    assert calculating.text.title == "Calculating a new energy plan"
    assert planning_notifications(replace(state, status="ready"), lambda _reason: None) == ()


def test_lifecycle_hint_expires_when_no_startup_is_reported():
    """An unreachable Home Assistant cannot leave a confirmed shutdown banner indefinitely."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.home_assistant.lifecycle import HomeAssistantLifecycle

    async def scenario():
        clock = [NOW]
        lifecycle = HomeAssistantLifecycle(clock=lambda: clock[0])
        app = web.Application()
        lifecycle.register(app)
        async with TestClient(TestServer(app)) as client:
            assert lifecycle.notice() is None
            response = await client.post("/v1/ha/lifecycle", json={"state": "stopping"})
            assert response.status == 200
            assert lifecycle.notice().text.title == "Home Assistant is stopping"
            clock[0] += timedelta(minutes=5)
            assert lifecycle.notice() is None

    asyncio.run(scenario())
