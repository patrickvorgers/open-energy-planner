"""Preserve the public app entrypoint and explicitly controlled Supervisor ingress startup."""

import sys

import pytest

from open_energy_planner import app
from open_energy_planner.runtime import entrypoint


@pytest.mark.parametrize("supervised", [False, True])
def test_public_demo_entrypoint_preserves_startup_and_ingress(monkeypatch, tmp_path, supervised):
    """The old module delegates without changing demo identity, token ownership, address or ingress trust."""
    calls = []
    application = object()

    def create(directory, token, **options):
        """Capture startup composition without starting services or writing persistent state."""
        calls.append((directory, token, options))
        return application

    monkeypatch.setattr(entrypoint, "create_managed_app", create)
    monkeypatch.setattr(entrypoint, "configure_logging", lambda: None)
    monkeypatch.setattr(
        entrypoint.web, "run_app", lambda result, **options: calls.append((result, options))
    )
    monkeypatch.setenv("OEP_API_TOKEN", "explicit-local-test-token")
    if supervised:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "local-supervisor-fixture")
    else:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "open_energy_planner.app",
            "--demo",
            "--data",
            str(tmp_path),
            "--host",
            "127.0.0.1",
            "--port",
            "8100",
        ],
    )
    app.main()
    directory, token, options = calls[0]
    assert directory == str(tmp_path)
    assert token == "explicit-local-test-token"
    assert options == {
        "factory": entrypoint.create_app,
        "source": "synthetic",
        "price_factory": None,
        "weather_factory": None,
        "solar_weather_factory": None,
        "discovery_options": None,
        "ingress_peer": "172.30.32.2" if supervised else None,
    }
    assert calls[1] == (
        application,
        {"host": "127.0.0.1", "port": 8100, "shutdown_timeout": 60, "print": None},
    )
