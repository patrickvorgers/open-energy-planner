"""Keep Energy membership and database preflight exception values behind their HTTP boundary."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.demand import api as demand_api
from open_energy_planner.runtime import maintenance
from open_energy_planner.solar import api as solar_api
from open_energy_planner.storage.adapters.lifecycle.maintenance import DestinationCheckError

PRIVATE_FAILURE = (
    "postgresql://private-user:SECRET_DATABASE_PASSWORD@private-host.invalid/archive "
    "token=SECRET_API_TOKEN"
)


def validation_route(kind, reject_validation, tmp_path, monkeypatch):
    """Use real registered handlers and parsing, replacing only the validation service that fails."""
    app = web.Application()
    lock = asyncio.Lock()
    service = SimpleNamespace(configuration=None, job=lambda _now: None)
    catalogue = SimpleNamespace(
        validate_membership=reject_validation,
        validate_panel_settings=reject_validation if kind == "solar-panels" else None,
    )
    if kind == "demand":
        demand_api.register(app, service, None, object(), catalogue, lock, None, None)
        return (
            app,
            "PUT",
            "/v1/demand",
            {
                "timezone": "Europe/Amsterdam",
                "annual_kwh": 3650,
                "terms": [{"entity_id": "sensor.house", "sign": 1}],
            },
            {},
        )
    if kind.startswith("solar"):
        from open_energy_planner.notifications.api import NOTIFICATIONS

        app[NOTIFICATIONS] = SimpleNamespace(register=lambda *_arguments, **_keywords: None)
        if kind == "solar-panels":
            catalogue.validate_membership = lambda *_arguments: None
        solar_api.register(app, service, object(), lock, None, None, None, catalogue)
        return app, "PUT", "/v1/solar", {"sources": [], "inverters": []}, {}
    token = "synthetic-maintenance-token-0123456789"
    monkeypatch.setattr(maintenance, "check_destination", reject_validation)
    from open_energy_planner.app import create_app

    app = maintenance.create_managed_app(
        tmp_path, token, factory=create_app, source="home_assistant"
    )
    return (
        app,
        "POST",
        "/v1/storage/migration/check",
        {"url": None},
        {"Authorization": f"Bearer {token}"},
    )


@pytest.mark.parametrize("kind", ["demand", "solar", "solar-panels", "storage"])
@pytest.mark.parametrize(
    "payload",
    [
        PRIVATE_FAILURE,
        "Device membership is managed in the Home Assistant Energy dashboard " + PRIVATE_FAILURE,
    ],
)
def test_unknown_validation_failure_keeps_http_and_logs_private(
    kind, payload, tmp_path, monkeypatch, caplog
):
    """Unrecognized exceptions and lookalike safe messages retain stack locations but never values."""

    def reject_validation(*_arguments):
        """Simulate a service defect containing private connection information."""
        raise (DestinationCheckError if kind == "storage" else ValueError)(payload)

    async def scenario():
        """Send valid JSON through the actual route, including maintenance authentication."""
        app, method, path, body, headers = validation_route(
            kind, reject_validation, tmp_path, monkeypatch
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.request(method, path, json=body, headers=headers)
            assert response.status == 400
            text = await response.text()
            assert "Reload" in text or "Check the destination" in text
            assert "SECRET" not in text and "private-host.invalid" not in text

    with caplog.at_level(logging.DEBUG, logger="open_energy_planner"):
        asyncio.run(scenario())
    assert "reject_validation" in caplog.text
    assert "test_configuration_error_privacy.py:" in caplog.text
    assert "SECRET" not in caplog.text and "private-host.invalid" not in caplog.text


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("demand", "Complete the Home Assistant Energy dashboard first"),
        ("demand", "Timezone is managed in the Home Assistant installation settings"),
        ("demand", "Device membership is managed in the Home Assistant Energy dashboard"),
        ("solar", "Complete the Home Assistant Energy dashboard first"),
        ("solar", "Device membership is managed in the Home Assistant Energy dashboard"),
        ("solar-panels", "Wait for complete Home Assistant panel settings"),
        ("solar-panels", "Panel settings are managed in Home Assistant. Reload before saving"),
        ("solar-panels", "The solar forecast link changed. Reload before saving"),
        (
            "storage",
            "Source and destination are the same database. Choose a different destination.",
        ),
        (
            "storage",
            "Connection check failed. Check the destination, credentials and database permissions. Source and destination must differ.",
        ),
    ],
)
def test_owned_validation_preserves_exact_recovery_advice(
    kind, message, tmp_path, monkeypatch, caplog
):
    """All current domain validation explanations remain actionable without unexpected-error logs."""

    def reject_validation(*_arguments):
        """Represent a known domain rejection with its independently specified public text."""
        raise (DestinationCheckError if kind == "storage" else ValueError)(message)

    async def scenario():
        """Check the real HTTP response rather than importing a production message catalogue."""
        app, method, path, body, headers = validation_route(
            kind, reject_validation, tmp_path, monkeypatch
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.request(method, path, json=body, headers=headers)
            assert response.status == 400
            assert await response.text() == message

    with caplog.at_level(logging.DEBUG, logger="open_energy_planner"):
        asyncio.run(scenario())
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
