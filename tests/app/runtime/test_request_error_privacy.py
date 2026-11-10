"""Keep service failures private at real HTTP boundaries without hiding safe validation advice."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.appliances.cycles import api as cycles_api
from open_energy_planner.appliances.ev import api as ev_api
from open_energy_planner.appliances.thermal import api as thermal_api
from open_energy_planner.forecast_quality import api as forecast_quality_api
from open_energy_planner.forecast_quality.history import HistoryArchive
from open_energy_planner.home_assistant.catalogue import HACatalogue
from open_energy_planner.prices import api as prices_api
from open_energy_planner.savings import api as savings_api
from open_energy_planner.savings.runtime import SavingsRuntime
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

PRIVATE_FAILURE = (
    "postgresql://private-user:SECRET_DATABASE_PASSWORD@private-host.invalid/archive "
    "token=SECRET_API_TOKEN"
)


def failure_route(kind, reject_operation, *, project_document=None):
    """Register the actual transport while substituting only its independently owned service call.

    Synchronous appliance calls still use the real cancellation-safe off-loop
    writer. The price route retains its real asynchronous service boundary.
    These tests cover error projection after admission. Authentication and
    actual durable storage retain their separate application/contract tests.
    """
    app = web.Application()
    lock = asyncio.Lock()
    repository = SimpleNamespace(state={"enabled": True, "plan": None})
    catalogue = HACatalogue()
    catalogue.value = {"timezone": "Europe/Amsterdam"}
    service = SimpleNamespace(
        revision="retained-revision",
        configuration_version="retained-configuration",
        configure=reject_operation,
        document=project_document,
        state={"points": [], "stores": []},
        consumers=SimpleNamespace(selected=[]),
    )
    service.ev = service
    if kind == "cycles":
        cycles_api.register(app, service, repository, None, lock, service, service)
        return (
            app,
            "POST",
            "/v1/appliances/device",
            {
                "entity_id": "sensor.appliance_energy",
                "shiftable": True,
                "expected_version": "retained-revision",
            },
        )
    if kind == "ev":
        ev_api.register(app, service, catalogue, repository, None, lock, service)
        return (
            app,
            "POST",
            "/v1/ev/configuration",
            {
                "configuration": {"points": []},
                "expected_version": "retained-configuration",
            },
        )
    if kind == "thermal":
        thermal_api.register(app, service, catalogue, repository, None, lock)
        return (
            app,
            "POST",
            "/v1/thermal/configuration",
            {
                "configuration": {"stores": []},
                "expected_version": "retained-configuration",
            },
        )

    async def save_preferences(*arguments):
        """Fail at the same asynchronous service boundary used by stored price preferences."""
        reject_operation(*arguments)

    service.save_preferences = save_preferences
    prices_api.register(app, lambda: service, None, None, lock)
    return (
        app,
        "PUT",
        "/v1/prices/preferences",
        {
            "preferences": {},
            "configuration_version": "retained-configuration",
            "market_area": "NL",
        },
    )


@pytest.mark.parametrize("kind", ["cycles", "ev", "thermal", "prices"])
@pytest.mark.parametrize("error_type", [ValueError, TypeError, KeyError])
def test_unknown_service_errors_keep_secrets_out_of_http_and_diagnostics(kind, error_type, caplog):
    """A service defect may contain driver secrets. Return safe advice and retain only stack locations."""

    def reject_service_operation(*_arguments, **_keywords):
        """Represent a failure below the HTTP boundary, after its request schema was accepted."""
        raise error_type(PRIVATE_FAILURE)

    async def scenario():
        """Send real JSON through the registered route without a production connection or database."""
        app, method, path, body = failure_route(kind, reject_service_operation)
        async with TestClient(TestServer(app)) as client:
            response = await client.request(method, path, json=body)
            assert response.status >= 400
            text = await response.text()
            assert text.strip()
            assert "SECRET_DATABASE_PASSWORD" not in text
            assert "SECRET_API_TOKEN" not in text
            assert "private-host.invalid" not in text

    with caplog.at_level(logging.DEBUG, logger="open_energy_planner"):
        asyncio.run(scenario())
    failures = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert failures
    assert error_type.__name__ in caplog.text
    assert "reject_service_operation" in caplog.text
    assert "test_request_error_privacy.py:" in caplog.text
    assert "SECRET_DATABASE_PASSWORD" not in caplog.text
    assert "SECRET_API_TOKEN" not in caplog.text
    assert "private-host.invalid" not in caplog.text


@pytest.mark.parametrize(
    ("kind", "safe_message"),
    [
        ("cycles", "Appliance settings changed. Reload before saving"),
        ("ev", "EV settings changed. Refresh before saving"),
        ("thermal", "Thermal settings changed. Refresh before saving"),
        ("prices", "Price source preferences changed. Reload before saving"),
    ],
)
def test_known_service_validation_keeps_actionable_recovery_advice(kind, safe_message, caplog):
    """A recognized stale-write rejection stays a useful 400 response rather than a software-failure alert."""

    def reject_stale_write(*_arguments, **_keywords):
        """Represent the service's explicit safe validation at the same write boundary."""
        raise ValueError(safe_message)

    async def scenario():
        """Exercise the real handler's validation mapping through HTTP."""
        app, method, path, body = failure_route(kind, reject_stale_write)
        async with TestClient(TestServer(app)) as client:
            response = await client.request(method, path, json=body)
            assert response.status == 400
            assert await response.text() == safe_message

    with caplog.at_level(logging.DEBUG, logger="open_energy_planner"):
        asyncio.run(scenario())
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.parametrize("kind", ["cycles", "ev", "thermal", "prices"])
def test_response_projection_failure_never_retries_an_acknowledged_write(kind, tmp_path, caplog):
    """A response can fail after commit. Keep that single durable write and expose no private details."""
    repository = SqlPlanRepository(tmp_path)
    documents = SqlDocuments(repository.engine, "planning")
    calls = []

    def acknowledge_write(*_arguments, **_keywords):
        """Commit real test evidence once before returning control to response construction."""
        calls.append(kind)
        documents.write("privacy-write.json", {"operation": kind, "count": len(calls)})

    def reject_projection(*_arguments, **_keywords):
        """Fail only after the registered handler's actual write acknowledgement boundary."""
        assert calls == [kind]
        raise ValueError(PRIVATE_FAILURE)

    async def scenario():
        """Exercise post-write projection over real HTTP and read back the committed evidence."""
        app, method, path, body = failure_route(
            kind, acknowledge_write, project_document=reject_projection
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.request(method, path, json=body)
            assert response.status >= 400
            text = await response.text()
            assert text.strip()
            assert "SECRET_DATABASE_PASSWORD" not in text
            assert "SECRET_API_TOKEN" not in text
            assert "private-host.invalid" not in text
        assert calls == [kind]
        assert SqlDocuments(repository.engine, "planning").read("privacy-write.json") == {
            "operation": kind,
            "count": 1,
        }

    try:
        with caplog.at_level(logging.DEBUG, logger="open_energy_planner"):
            asyncio.run(scenario())
    finally:
        repository.close()
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    assert "ValueError" in caplog.text
    assert "reject_projection" in caplog.text
    assert "test_request_error_privacy.py:" in caplog.text
    assert "SECRET_DATABASE_PASSWORD" not in caplog.text
    assert "SECRET_API_TOKEN" not in caplog.text
    assert "private-host.invalid" not in caplog.text
    assert not any(record.levelno == logging.INFO for record in caplog.records)


@pytest.mark.parametrize("kind", ["forecast_history", "settlement"])
@pytest.mark.parametrize("error_type", [ValueError, TypeError, KeyError])
def test_history_receipt_errors_never_disclose_driver_details(
    kind, error_type, tmp_path, monkeypatch, caplog
):
    """Real history ingress keeps unexpected receipt failures private and records a diagnosable stack."""
    repository = SqlPlanRepository(tmp_path)
    calls = []

    def pending_request(_runtime, *_arguments):
        """Provide one independently fixed pending identity so the real request reaches receipt."""
        return {"version": "accepted-request"}

    def reject_receipt(_runtime, *_arguments):
        """Represent an unknown failure at the actual off-loop receipt boundary."""
        calls.append(kind)
        raise error_type(PRIVATE_FAILURE)

    async def scenario():
        """Keep real HTTP routing, worker lifetimes and stores while injecting the receipt defect."""
        app = web.Application()
        lock = asyncio.Lock()
        catalogue = SimpleNamespace(value=None)
        demand = SimpleNamespace(configuration=None, consumers=SimpleNamespace(selected=()))
        solar = SimpleNamespace(configuration=None)
        if kind == "forecast_history":
            monkeypatch.setattr(HistoryArchive, "request", pending_request)
            monkeypatch.setattr(HistoryArchive, "receive", reject_receipt)
            forecast_quality_api.register(app, repository, demand, solar, catalogue, lock)
            path = "/v1/forecast-quality/history"
        else:
            monkeypatch.setattr(SavingsRuntime, "request", pending_request)
            monkeypatch.setattr(SavingsRuntime, "receive", reject_receipt)
            savings_api.register(
                app,
                repository,
                SimpleNamespace(current=None),
                SimpleNamespace(current=None),
                SimpleNamespace(current=None),
                solar,
                demand,
                lock,
                catalogue,
                lambda: None,
                mutation_lock=asyncio.Lock(),
            )
            path = "/v1/settlement"
        async with TestClient(TestServer(app)) as client:
            response = await client.post(path, json={"request_version": "accepted-request"})
            assert response.status >= 400
            text = await response.text()
            assert text.strip()
            assert "SECRET_DATABASE_PASSWORD" not in text
            assert "SECRET_API_TOKEN" not in text
            assert "private-host.invalid" not in text
        assert calls == [kind]

    try:
        with caplog.at_level(logging.DEBUG, logger="open_energy_planner"):
            asyncio.run(scenario())
    finally:
        repository.close()
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    assert error_type.__name__ in caplog.text
    assert "reject_receipt" in caplog.text
    assert "test_request_error_privacy.py:" in caplog.text
    assert "SECRET_DATABASE_PASSWORD" not in caplog.text
    assert "SECRET_API_TOKEN" not in caplog.text
    assert "private-host.invalid" not in caplog.text
