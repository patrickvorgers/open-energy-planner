"""Durable autonomous arrival requests and SoC evidence on the shared database matrix."""

from datetime import timedelta

import pytest

from open_energy_planner.appliances.ev.service import EVService
from tests.app.ev.support.devices import CAR, NOW, POINT, catalogue, observations, settings
from tests.database.support import ev as ev_support

ev_service = ev_support.ev_service


def single_car(service):
    """Allow one car without requiring optional vehicle provider signals."""
    configuration = settings()
    configuration["points"][0]["vehicle_ids"] = [CAR]
    service.configure(configuration, catalogue(), NOW)


def test_arrival_creates_one_request_and_cancellation_survives_restart(ev_service):
    """Arrival uses defaults once. Polling and app restart cannot undo the user's cancellation."""
    single_car(ev_service)
    ev_service.receive(observations(ev_service), NOW, "Europe/Amsterdam")
    request = ev_service.document(NOW, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
    assert request["target_soc"] == 80
    assert request["departure"] == "2026-10-27T06:00:00+00:00"
    assert ev_service.state["assignments"][POINT]["vehicle_id"] == CAR
    assert ev_service.work(NOW, "Europe/Amsterdam")[0].required_kwh == 18
    ev_service.cancel_request(request["id"], request["version"], NOW, CAR)
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    later = NOW + timedelta(seconds=10)
    restored.receive(observations(restored, later), later, "Europe/Amsterdam")
    assert restored.work(later, "Europe/Amsterdam") == ()
    assert len(restored.state["requests"]) == 1
    assert restored.state["requests"][request["id"]]["status"] == "cancelled"


def test_meter_estimate_survives_restart_without_reviving_stale_connection(ev_service):
    """2.3 measured AC kWh gives 1.84 battery kWh. Restart retains it but requires a fresh session report."""
    single_car(ev_service)
    first = observations(ev_service)
    first["points"][0]["energy_kwh"] = 100
    ev_service.receive(first, NOW, "Europe/Amsterdam")
    later = NOW + timedelta(hours=1)
    report = observations(ev_service, later, soc=None)
    report["points"][0]["energy_kwh"] = 102.3
    ev_service.receive(report, later, "Europe/Amsterdam")
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    estimate = restored.vehicle_readings(later)[CAR]
    assert estimate["soc"] == pytest.approx(50 + 1.84 / 60 * 100)
    assert estimate["observed_at"] == NOW.isoformat()
    assert estimate["basis"] == "estimated_from_meter"
    assert restored.work(later, "Europe/Amsterdam") == ()
    report["at"] = (later + timedelta(seconds=1)).isoformat()
    restored.receive(report, later + timedelta(seconds=1), "Europe/Amsterdam")
    assert restored.work(later + timedelta(seconds=1), "Europe/Amsterdam")[
        0
    ].initial_soc == pytest.approx(estimate["soc"])


def test_failed_observation_write_preserves_memory_and_durable_telemetry(ev_service, monkeypatch):
    """A failed commit cannot publish a new SoC anchor in memory or after reconstruction."""
    single_car(ev_service)
    ev_service.receive(observations(ev_service), NOW, "Europe/Amsterdam")
    original_write = ev_service.documents.write

    def fail_telemetry(key, value):
        """Fail only the mutable telemetry pointer after preceding writes in this transaction."""
        if key == "ev-telemetry.json":
            raise RuntimeError("Synthetic storage failure")
        return original_write(key, value)

    monkeypatch.setattr(ev_service.documents, "write", fail_telemetry)
    later = NOW + timedelta(seconds=5)
    with pytest.raises(RuntimeError, match="Synthetic"):
        ev_service.receive(observations(ev_service, later, soc=60), later, "Europe/Amsterdam")
    assert ev_service.vehicle_readings(later)[CAR]["soc"] == 50
    assert ev_service.report["at"] == NOW.isoformat()
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    assert restored.vehicle_readings(later)[CAR]["soc"] == 50
